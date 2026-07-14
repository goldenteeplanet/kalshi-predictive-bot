from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import ForecastMemory, MarketMemory, TradeMemory
from kalshi_predictor.feature_discovery.contracts import (
    ALLOWED_FEATURE_SOURCES,
    DatasetManifest,
    DiscoveryDatasetRow,
    FeatureDiscoveryConfig,
    SourceWatermarks,
    checksum_payload,
    stable_phase_3q_id,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime

UNAVAILABLE_SOURCES = [
    "authoritative_exchange_fee_schedule",
    "versioned_counterfactual_fill_model",
    "dedicated_production_feature_registry",
    "protected_holdout_registry",
]


def build_phase3o_discovery_dataset(
    session: Session,
    *,
    training_as_of: datetime,
    config: FeatureDiscoveryConfig,
) -> tuple[list[DiscoveryDatasetRow], DatasetManifest]:
    config.validate()
    cutoff = _utc(training_as_of)
    excluded: Counter[str] = Counter()
    rows: list[DiscoveryDatasetRow] = []
    rows_total = 0

    latest_forecasts = _latest_forecast_records(session)
    rows_total += len(latest_forecasts)
    for forecast in latest_forecasts:
        row, reason = _row_from_forecast(forecast, training_as_of=cutoff)
        if row is None:
            excluded[reason or "forecast_not_eligible"] += 1
            continue
        rows.append(row)

    latest_trades = _latest_trade_records(session)
    rows_total += len(latest_trades)
    for trade in latest_trades:
        row, reason = _row_from_trade(trade, training_as_of=cutoff)
        if row is None:
            excluded[reason or "trade_not_eligible"] += 1
            continue
        rows.append(row)

    watermarks = _source_watermarks(session)
    manifest_payload = {
        "schema_version": "3Q.1",
        "training_as_of": cutoff.isoformat(),
        "data_mode": config.data_mode,
        "rows_total": rows_total,
        "rows_included": len(rows),
        "excluded_counts": dict(sorted(excluded.items())),
        "source_watermarks": watermarks.as_payload(),
        "allowed_feature_sources": sorted(ALLOWED_FEATURE_SOURCES),
        "unavailable_sources": UNAVAILABLE_SOURCES,
    }
    manifest_hash = checksum_payload(manifest_payload)
    manifest = DatasetManifest(
        manifest_id=stable_phase_3q_id("dataset_manifest", manifest_hash),
        manifest_hash=manifest_hash,
        training_as_of=cutoff,
        data_mode=config.data_mode,
        rows_total=rows_total,
        rows_included=len(rows),
        excluded_counts=dict(sorted(excluded.items())),
        source_watermarks=watermarks,
        unavailable_sources=list(UNAVAILABLE_SOURCES),
    )
    return rows, manifest


def _latest_forecast_records(session: Session) -> list[ForecastMemory]:
    records = session.scalars(
        select(ForecastMemory).order_by(
            ForecastMemory.forecast_id,
            ForecastMemory.event_sequence.desc(),
            ForecastMemory.recorded_at.desc(),
        )
    ).all()
    latest: dict[str, ForecastMemory] = {}
    for record in records:
        latest.setdefault(record.forecast_id, record)
    return list(latest.values())


def _latest_trade_records(session: Session) -> list[TradeMemory]:
    records = session.scalars(
        select(TradeMemory).order_by(
            TradeMemory.trade_id,
            TradeMemory.event_sequence.desc(),
            TradeMemory.recorded_at.desc(),
        )
    ).all()
    latest: dict[str, TradeMemory] = {}
    for record in records:
        latest.setdefault(record.trade_id, record)
    return list(latest.values())


def _row_from_forecast(
    forecast: ForecastMemory,
    *,
    training_as_of: datetime,
) -> tuple[DiscoveryDatasetRow | None, str | None]:
    decision_time = _utc(
        forecast.forecast_generated_at
        or forecast.forecast_valid_from
        or forecast.observed_at
        or forecast.event_time
    )
    feature_observed = _utc(forecast.feature_observed_through or decision_time)
    if feature_observed > decision_time:
        return None, "feature_observed_after_decision"
    label_time = forecast.label_available_at or forecast.outcome_finalized_at
    if label_time is None:
        return None, "label_not_finalized"
    label_available_at = _utc(label_time)
    if label_available_at > training_as_of:
        return None, "label_after_training_cutoff"
    if forecast.forecast_outcome_status.upper() not in {"FINAL", "FINALIZED", "SETTLED"}:
        return None, "forecast_outcome_not_final"

    outcome = _forecast_outcome(forecast)
    if outcome is None:
        return None, "forecast_label_missing"
    features = _forecast_features(forecast)
    if not features:
        return None, "no_allowed_feature_values"
    row_id = stable_phase_3q_id("forecast_row", forecast.forecast_memory_event_id)
    return (
        DiscoveryDatasetRow(
            row_id=row_id,
            analysis_unit="FORECAST",
            source_memory_id=forecast.forecast_memory_event_id,
            instrument_id=forecast.instrument_id,
            category_id=forecast.category_id,
            model_id=forecast.primary_model_id,
            execution_mode="FORECAST_ONLY",
            decision_timestamp=decision_time,
            feature_observed_through=feature_observed,
            label_available_at=label_available_at,
            label_interval_start=decision_time,
            label_interval_end=_utc(forecast.forecast_target_at or label_available_at),
            outcome_name="forecast_direction_profitable_proxy",
            outcome_value=outcome,
            net_pnl=None,
            total_cost=None,
            feature_values=features,
            feature_lineage=decode_json(forecast.feature_lineage_json),
            source_quality_flags=_json_list(forecast.data_quality_flags_json),
        ),
        None,
    )


def _row_from_trade(
    trade: TradeMemory,
    *,
    training_as_of: datetime,
) -> tuple[DiscoveryDatasetRow | None, str | None]:
    decision_time = _utc(trade.event_time)
    label_time = trade.outcome_finalized_at or trade.settled_at
    if label_time is None:
        return None, "trade_outcome_not_final"
    label_available_at = _utc(label_time)
    if label_available_at > training_as_of:
        return None, "label_after_training_cutoff"
    if not trade.net_pnl:
        return None, "net_pnl_missing"
    net_pnl = to_decimal(trade.net_pnl)
    if net_pnl is None:
        return None, "net_pnl_invalid"
    total_cost = to_decimal(trade.total_cost)
    if total_cost is None:
        return None, "cost_component_incomplete"
    features = _trade_features(trade)
    if not features:
        return None, "no_allowed_feature_values"
    row_id = stable_phase_3q_id("trade_row", trade.trade_memory_event_id)
    return (
        DiscoveryDatasetRow(
            row_id=row_id,
            analysis_unit="TRADE",
            source_memory_id=trade.trade_memory_event_id,
            instrument_id=trade.instrument_id,
            category_id=trade.category_id,
            model_id=trade.model_id,
            execution_mode=trade.execution_mode,
            decision_timestamp=decision_time,
            feature_observed_through=decision_time,
            label_available_at=label_available_at,
            label_interval_start=decision_time,
            label_interval_end=label_available_at,
            outcome_name="net_profitable_after_costs",
            outcome_value=Decimal("1") if net_pnl > 0 else Decimal("0"),
            net_pnl=net_pnl,
            total_cost=total_cost,
            feature_values=features,
            feature_lineage={"source": "trade_memory", "event_id": trade.trade_memory_event_id},
            source_quality_flags=_json_list(trade.data_quality_flags_json),
        ),
        None,
    )


def _forecast_outcome(forecast: ForecastMemory) -> Decimal | None:
    if forecast.direction_correct is not None:
        return Decimal("1") if int(forecast.direction_correct) == 1 else Decimal("0")
    actual = to_decimal(forecast.actual_value)
    predicted = to_decimal(forecast.predicted_probability)
    if actual is None or predicted is None:
        return None
    direction_yes = predicted >= Decimal("0.5")
    actual_yes = actual >= Decimal("0.5")
    return Decimal("1") if direction_yes == actual_yes else Decimal("0")


def _forecast_features(forecast: ForecastMemory) -> dict[str, Decimal]:
    values = {
        "predicted_probability": forecast.predicted_probability,
        "confidence_score": forecast.confidence_score,
        "opportunity_score": forecast.opportunity_score,
        "liquidity_score": forecast.liquidity_score,
        "risk_adjusted_expected_value": forecast.risk_adjusted_expected_value,
        "phase_3m_composite_score": forecast.phase_3m_composite_score,
        "phase_3m_proposed_contracts": forecast.phase_3m_proposed_contracts,
        "phase_3n_approved_contracts": forecast.phase_3n_approved_contracts,
    }
    return _decimal_feature_map(values)


def _trade_features(trade: TradeMemory) -> dict[str, Decimal]:
    values = {
        "confidence_score": trade.confidence_score,
        "opportunity_score": trade.opportunity_score,
        "risk_adjusted_expected_value": trade.risk_adjusted_expected_value,
        "phase_3m_proposed_contracts": trade.phase_3m_proposed_contracts,
        "phase_3n_approved_contracts": trade.phase_3n_approved_contracts,
    }
    return _decimal_feature_map(values)


def _decimal_feature_map(values: dict[str, Any]) -> dict[str, Decimal]:
    output: dict[str, Decimal] = {}
    for key, value in values.items():
        if key not in ALLOWED_FEATURE_SOURCES:
            continue
        decimal = to_decimal(value)
        if decimal is None or not decimal.is_finite():
            continue
        output[key] = decimal
    return output


def _source_watermarks(session: Session) -> SourceWatermarks:
    return SourceWatermarks(
        market_memory_latest=_iso_or_none(session.scalar(select(func.max(MarketMemory.recorded_at)))),
        forecast_memory_latest=_iso_or_none(
            session.scalar(select(func.max(ForecastMemory.recorded_at)))
        ),
        trade_memory_latest=_iso_or_none(session.scalar(select(func.max(TradeMemory.recorded_at)))),
        market_memory_rows=int(session.scalar(select(func.count()).select_from(MarketMemory)) or 0),
        forecast_memory_rows=int(
            session.scalar(select(func.count()).select_from(ForecastMemory)) or 0
        ),
        trade_memory_rows=int(session.scalar(select(func.count()).select_from(TradeMemory)) or 0),
    )


def _utc(value: datetime | str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Invalid datetime: {value}")
    return parsed.astimezone(UTC)


def _iso_or_none(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _json_list(value: str | None) -> list[str]:
    decoded = decode_json(value)
    if isinstance(decoded.get("flags"), list):
        return [str(item) for item in decoded["flags"]]
    if isinstance(decoded.get("reason_codes"), list):
        return [str(item) for item in decoded["reason_codes"]]
    try:
        raw = __import__("json").loads(value or "[]")
    except ValueError:
        return []
    return [str(item) for item in raw] if isinstance(raw, list) else []
