from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    ForecastMemory,
    MarketMemory,
    MemoryEventQuarantine,
    TradeMemory,
)
from kalshi_predictor.memory.contracts import (
    FORECAST_EVENT_TYPES,
    MARKET_SNAPSHOT_TYPES,
    STORE_FORECAST,
    STORE_MARKET,
    STORE_TRADE,
    TRADE_EVENT_TYPES,
    MemoryWriteReceipt,
    canonical_json,
    ensure_event_type,
    now_utc,
    payload_hash,
    stable_id,
)

logger = logging.getLogger(__name__)


def memory_capture_enabled(settings: Settings | None = None) -> bool:
    resolved = settings or get_settings()
    return (
        resolved.phase_3o_market_memory_enabled
        and resolved.phase_3o_market_memory_mode != "disabled"
    )


def write_market_memory(
    session: Session,
    values: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> MemoryWriteReceipt:
    if not memory_capture_enabled(settings):
        return _disabled(STORE_MARKET, str(values.get("idempotency_key") or "disabled"))
    payload = _prepare_payload(values, "market_memory_id")
    payload["snapshot_type"] = ensure_event_type(
        str(payload["snapshot_type"]),
        MARKET_SNAPSHOT_TYPES,
        field="snapshot_type",
    )
    payload["event_type"] = payload.get("event_type") or payload["snapshot_type"]
    payload["payload_hash"] = payload_hash(_hash_payload(payload))
    payload["market_memory_id"] = payload.get("market_memory_id") or stable_id(
        STORE_MARKET,
        payload["idempotency_key"],
    )
    return _write(
        session,
        store=STORE_MARKET,
        model=MarketMemory,
        id_field="market_memory_id",
        values=payload,
    )


def write_forecast_memory(
    session: Session,
    values: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> MemoryWriteReceipt:
    if not memory_capture_enabled(settings):
        return _disabled(STORE_FORECAST, str(values.get("idempotency_key") or "disabled"))
    payload = _prepare_payload(values, "forecast_memory_event_id")
    payload["event_type"] = ensure_event_type(str(payload["event_type"]), FORECAST_EVENT_TYPES)
    payload["payload_hash"] = payload_hash(_hash_payload(payload))
    payload["forecast_memory_event_id"] = payload.get("forecast_memory_event_id") or stable_id(
        STORE_FORECAST,
        payload["idempotency_key"],
    )
    return _write(
        session,
        store=STORE_FORECAST,
        model=ForecastMemory,
        id_field="forecast_memory_event_id",
        values=payload,
    )


def write_trade_memory(
    session: Session,
    values: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> MemoryWriteReceipt:
    if not memory_capture_enabled(settings):
        return _disabled(STORE_TRADE, str(values.get("idempotency_key") or "disabled"))
    payload = _prepare_payload(values, "trade_memory_event_id")
    payload["event_type"] = ensure_event_type(str(payload["event_type"]), TRADE_EVENT_TYPES)
    payload["payload_hash"] = payload_hash(_hash_payload(payload))
    payload["trade_memory_event_id"] = payload.get("trade_memory_event_id") or stable_id(
        STORE_TRADE,
        payload["idempotency_key"],
    )
    return _write(
        session,
        store=STORE_TRADE,
        model=TradeMemory,
        id_field="trade_memory_event_id",
        values=payload,
    )


def latest_market_memory_for_source(session: Session, source_event_id: str) -> MarketMemory | None:
    return session.scalar(
        select(MarketMemory)
        .where(MarketMemory.source_event_id == source_event_id)
        .order_by(desc(MarketMemory.recorded_at))
        .limit(1)
    )


def latest_market_memory_for_instrument(
    session: Session,
    instrument_id: str,
) -> MarketMemory | None:
    return session.scalar(
        select(MarketMemory)
        .where(MarketMemory.instrument_id == instrument_id)
        .order_by(desc(MarketMemory.market_event_time), desc(MarketMemory.recorded_at))
        .limit(1)
    )


def latest_forecast_memory_event(
    session: Session,
    forecast_id: str,
) -> ForecastMemory | None:
    return session.scalar(
        select(ForecastMemory)
        .where(ForecastMemory.forecast_id == forecast_id)
        .order_by(desc(ForecastMemory.event_sequence), desc(ForecastMemory.recorded_at))
        .limit(1)
    )


def forecast_timeline(session: Session, forecast_id: str) -> list[ForecastMemory]:
    return list(
        session.scalars(
            select(ForecastMemory)
            .where(ForecastMemory.forecast_id == forecast_id)
            .order_by(ForecastMemory.event_sequence, ForecastMemory.recorded_at)
        )
    )


def trade_timeline(session: Session, trade_id: str) -> list[TradeMemory]:
    return list(
        session.scalars(
            select(TradeMemory)
            .where(TradeMemory.trade_id == trade_id)
            .order_by(TradeMemory.event_sequence, TradeMemory.recorded_at)
        )
    )


def memory_status(session: Session) -> dict[str, Any]:
    return {
        "market_memory": _count(session, MarketMemory),
        "forecast_memory": _count(session, ForecastMemory),
        "trade_memory": _count(session, TradeMemory),
        "quarantine": _count(session, MemoryEventQuarantine),
        "latest_market_event": _latest_timestamp(session, MarketMemory.recorded_at),
        "latest_forecast_event": _latest_timestamp(session, ForecastMemory.recorded_at),
        "latest_trade_event": _latest_timestamp(session, TradeMemory.recorded_at),
    }


def _write(
    session: Session,
    *,
    store: str,
    model: type,
    id_field: str,
    values: dict[str, Any],
) -> MemoryWriteReceipt:
    existing = session.scalar(
        select(model).where(model.idempotency_key == values["idempotency_key"]).limit(1)
    )
    if existing is not None:
        event_id = str(getattr(existing, id_field))
        if existing.payload_hash == values["payload_hash"]:
            return MemoryWriteReceipt(
                store=store,
                status="duplicate",
                memory_event_id=event_id,
                idempotency_key=values["idempotency_key"],
                payload_hash=values["payload_hash"],
                message="Duplicate memory event ignored.",
            )
        _quarantine_conflict(
            session,
            store=store,
            existing=existing,
            values=values,
        )
        return MemoryWriteReceipt(
            store=store,
            status="conflict",
            memory_event_id=event_id,
            idempotency_key=values["idempotency_key"],
            payload_hash=values["payload_hash"],
            message="Conflicting idempotency key quarantined.",
        )
    allowed = set(model.__table__.columns.keys())
    record = model(**{key: value for key, value in values.items() if key in allowed})
    session.add(record)
    session.flush()
    return MemoryWriteReceipt(
        store=store,
        status="written",
        memory_event_id=str(getattr(record, id_field)),
        idempotency_key=values["idempotency_key"],
        payload_hash=values["payload_hash"],
        message="Memory event written.",
    )


def _prepare_payload(values: Mapping[str, Any], id_field: str) -> dict[str, Any]:
    payload = dict(values)
    payload.setdefault("event_sequence", 1)
    payload.setdefault("event_version", 1)
    payload.setdefault("schema_version", 1)
    payload.setdefault("recorded_at", now_utc())
    payload.setdefault("metadata_json", canonical_json(payload.get("metadata") or {}))
    payload.setdefault("event_payload_json", canonical_json(payload.get("event_payload") or {}))
    payload.setdefault(
        "data_quality_flags_json", canonical_json(payload.get("data_quality_flags") or [])
    )
    payload.setdefault("reason_codes_json", canonical_json(payload.get("reason_codes") or []))
    payload.setdefault(
        "phase_3n_reason_codes_json", canonical_json(payload.get("phase_3n_reason_codes") or [])
    )
    payload.setdefault("model_lineage_json", canonical_json(payload.get("model_lineage") or []))
    payload.setdefault("feature_lineage_json", canonical_json(payload.get("feature_lineage") or {}))
    payload.setdefault(
        "paper_fill_policy_json", canonical_json(payload.get("paper_fill_policy") or {})
    )
    payload.setdefault(
        "outcome_reason_codes_json", canonical_json(payload.get("outcome_reason_codes") or [])
    )
    payload.setdefault("feature_values_json", canonical_json(payload.get("feature_values") or {}))
    payload.pop("metadata", None)
    payload.pop("event_payload", None)
    payload.pop("data_quality_flags", None)
    payload.pop("reason_codes", None)
    payload.pop("phase_3n_reason_codes", None)
    payload.pop("model_lineage", None)
    payload.pop("feature_lineage", None)
    payload.pop("paper_fill_policy", None)
    payload.pop("outcome_reason_codes", None)
    payload.pop("feature_values", None)
    payload.pop("payload_hash", None)
    payload.setdefault(id_field, None)
    return payload


def _hash_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {
        "market_memory_id",
        "forecast_memory_event_id",
        "trade_memory_event_id",
        "payload_hash",
        "recorded_at",
    }
    return {key: value for key, value in payload.items() if key not in ignored}


def _quarantine_conflict(
    session: Session,
    *,
    store: str,
    existing: Any,
    values: Mapping[str, Any],
) -> None:
    row = MemoryEventQuarantine(
        store=store,
        idempotency_key=str(values["idempotency_key"]),
        attempted_payload_hash=str(values["payload_hash"]),
        existing_payload_hash=getattr(existing, "payload_hash", None),
        reason="MEMORY_WRITE_CONFLICT",
        source_component=values.get("source_component"),
        created_at=now_utc(),
        raw_json=canonical_json(values),
    )
    session.add(row)
    session.flush()
    logger.warning(
        "memory_event_quarantined",
        extra={
            "memory": {
                "store": store,
                "idempotency_key": values["idempotency_key"],
                "reason": "MEMORY_WRITE_CONFLICT",
            }
        },
    )


def _disabled(store: str, idempotency_key: str) -> MemoryWriteReceipt:
    return MemoryWriteReceipt(
        store=store,
        status="disabled",
        memory_event_id=None,
        idempotency_key=idempotency_key,
        payload_hash="",
        message="Phase 3O memory capture is disabled.",
    )


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _latest_timestamp(session: Session, column: Any) -> str:
    value = session.scalar(select(func.max(column)))
    return value.isoformat() if value else "n/a"
