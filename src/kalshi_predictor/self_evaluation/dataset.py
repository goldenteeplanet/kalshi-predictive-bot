from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import ForecastMemory, MarketMemory, TradeMemory
from kalshi_predictor.self_evaluation.contracts import (
    TradingSession,
    checksum_payload,
    stable_phase_3p_id,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

FINAL_FORECAST_STATUSES = {"FINAL", "FINALIZED", "SETTLED"}
FINAL_TRADE_STATUSES = {"FINAL", "FINALIZED", "SETTLED"}
FINAL_TRADE_EVENTS = {"SETTLEMENT_FINAL", "TRADE_OUTCOME_FINALIZED"}


@dataclass(frozen=True)
class EvaluationDataset:
    trading_session: TradingSession
    evaluation_as_of: datetime
    data_mode: str
    market_rows: list[MarketMemory]
    forecast_events: list[ForecastMemory]
    trade_events: list[TradeMemory]
    forecast_rows: list[ForecastMemory]
    trade_rows: list[TradeMemory]
    excluded_after_cutoff: dict[str, int]
    manifest: dict[str, Any]

    @property
    def finalized_forecasts(self) -> list[ForecastMemory]:
        return [row for row in self.forecast_rows if forecast_is_final(row, self.evaluation_as_of)]

    @property
    def pending_forecasts(self) -> list[ForecastMemory]:
        return [
            row
            for row in self.forecast_rows
            if not forecast_is_final(row, self.evaluation_as_of)
        ]

    @property
    def finalized_trades(self) -> list[TradeMemory]:
        return [row for row in self.trade_rows if trade_is_final(row, self.evaluation_as_of)]

    @property
    def open_trades(self) -> list[TradeMemory]:
        return [row for row in self.trade_rows if not trade_is_final(row, self.evaluation_as_of)]

    @property
    def source_references(self) -> list[dict[str, str]]:
        refs = []
        refs.extend(_source_ref("market_memory", row.market_memory_id) for row in self.market_rows)
        refs.extend(
            _source_ref("forecast_memory", row.forecast_memory_event_id)
            for row in self.forecast_events
        )
        refs.extend(
            _source_ref("trade_memory", row.trade_memory_event_id)
            for row in self.trade_events
        )
        return refs


def build_evaluation_dataset(
    session: Session,
    *,
    trading_session: TradingSession,
    evaluation_as_of: datetime,
    data_mode: str = "AS_OBSERVED",
) -> EvaluationDataset:
    cutoff = _require_datetime(evaluation_as_of)
    market_rows = _market_rows(
        session,
        trading_session=trading_session,
        cutoff=cutoff,
        data_mode=data_mode,
    )
    created_forecast_ids = _session_forecast_ids(
        session,
        trading_session=trading_session,
        cutoff=cutoff,
    )
    forecast_events = _forecast_events(session, created_forecast_ids, cutoff=cutoff)
    forecast_rows = _latest_by_key(forecast_events, "forecast_id")
    session_trade_ids = _session_trade_ids(
        session,
        trading_session=trading_session,
        cutoff=cutoff,
        forecast_ids=set(created_forecast_ids),
    )
    trade_events = _trade_events(session, session_trade_ids, cutoff=cutoff)
    trade_rows = _latest_by_key(trade_events, "trade_id")
    excluded = {
        "market_memory_after_cutoff": _excluded_market_count(
            session, trading_session=trading_session, cutoff=cutoff
        ),
        "forecast_memory_after_cutoff": _excluded_forecast_count(
            session, trading_session=trading_session, cutoff=cutoff
        ),
        "trade_memory_after_cutoff": _excluded_trade_count(
            session, trading_session=trading_session, cutoff=cutoff
        ),
    }
    manifest = _manifest(
        trading_session=trading_session,
        evaluation_as_of=cutoff,
        data_mode=data_mode,
        market_rows=market_rows,
        forecast_events=forecast_events,
        trade_events=trade_events,
        excluded=excluded,
    )
    return EvaluationDataset(
        trading_session=trading_session,
        evaluation_as_of=cutoff,
        data_mode=data_mode,
        market_rows=market_rows,
        forecast_events=forecast_events,
        trade_events=trade_events,
        forecast_rows=forecast_rows,
        trade_rows=trade_rows,
        excluded_after_cutoff=excluded,
        manifest=manifest,
    )


def forecast_is_final(row: ForecastMemory, evaluation_as_of: datetime) -> bool:
    status = (row.forecast_outcome_status or "").upper()
    if status not in FINAL_FORECAST_STATUSES:
        return False
    if row.label_available_at is None or _after(row.label_available_at, evaluation_as_of):
        return False
    if row.outcome_finalized_at is not None and _after(
        row.outcome_finalized_at,
        evaluation_as_of,
    ):
        return False
    return True


def trade_is_final(row: TradeMemory, evaluation_as_of: datetime) -> bool:
    event_type = (row.event_type or "").upper()
    status = (row.settlement_status or "").upper()
    if event_type not in FINAL_TRADE_EVENTS and status not in FINAL_TRADE_STATUSES:
        return False
    final_time = row.outcome_finalized_at or row.settled_at
    return final_time is not None and not _after(final_time, evaluation_as_of)


def _market_rows(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
    data_mode: str,
) -> list[MarketMemory]:
    return list(
        session.scalars(
            select(MarketMemory)
            .where(MarketMemory.recorded_at <= cutoff)
            .where(MarketMemory.market_event_time >= trading_session.evaluation_window_start)
            .where(MarketMemory.market_event_time <= trading_session.evaluation_window_end)
            .where(MarketMemory.data_mode == data_mode)
            .order_by(MarketMemory.market_event_time, MarketMemory.market_memory_id)
        )
    )


def _session_forecast_ids(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
) -> list[str]:
    rows = session.scalars(
        select(ForecastMemory.forecast_id)
        .where(ForecastMemory.recorded_at <= cutoff)
        .where(
            or_(
                and_(
                    ForecastMemory.forecast_generated_at.is_not(None),
                    ForecastMemory.forecast_generated_at
                    >= trading_session.evaluation_window_start,
                    ForecastMemory.forecast_generated_at
                    <= trading_session.evaluation_window_end,
                ),
                and_(
                    ForecastMemory.event_type == "FORECAST_CREATED",
                    ForecastMemory.event_time >= trading_session.evaluation_window_start,
                    ForecastMemory.event_time <= trading_session.evaluation_window_end,
                ),
            )
        )
        .order_by(ForecastMemory.forecast_id)
    )
    return sorted({row for row in rows})


def _forecast_events(
    session: Session,
    forecast_ids: list[str],
    *,
    cutoff: datetime,
) -> list[ForecastMemory]:
    if not forecast_ids:
        return []
    return list(
        session.scalars(
            select(ForecastMemory)
            .where(ForecastMemory.forecast_id.in_(forecast_ids))
            .where(ForecastMemory.recorded_at <= cutoff)
            .order_by(
                ForecastMemory.forecast_id,
                ForecastMemory.event_sequence,
                ForecastMemory.recorded_at,
            )
        )
    )


def _session_trade_ids(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
    forecast_ids: set[str],
) -> list[str]:
    conditions = [
        and_(
            TradeMemory.event_time >= trading_session.evaluation_window_start,
            TradeMemory.event_time <= trading_session.evaluation_window_end,
        )
    ]
    if forecast_ids:
        conditions.append(TradeMemory.forecast_id.in_(forecast_ids))
    rows = session.scalars(
        select(TradeMemory.trade_id)
        .where(TradeMemory.recorded_at <= cutoff)
        .where(or_(*conditions))
        .order_by(TradeMemory.trade_id)
    )
    return sorted({row for row in rows})


def _trade_events(
    session: Session,
    trade_ids: list[str],
    *,
    cutoff: datetime,
) -> list[TradeMemory]:
    if not trade_ids:
        return []
    return list(
        session.scalars(
            select(TradeMemory)
            .where(TradeMemory.trade_id.in_(trade_ids))
            .where(TradeMemory.recorded_at <= cutoff)
            .order_by(TradeMemory.trade_id, TradeMemory.event_sequence, TradeMemory.recorded_at)
        )
    )


def _excluded_market_count(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(MarketMemory)
            .where(MarketMemory.recorded_at > cutoff)
            .where(MarketMemory.market_event_time >= trading_session.evaluation_window_start)
            .where(MarketMemory.market_event_time <= trading_session.evaluation_window_end)
        )
        or 0
    )


def _excluded_forecast_count(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ForecastMemory)
            .where(ForecastMemory.recorded_at > cutoff)
            .where(ForecastMemory.event_time >= trading_session.evaluation_window_start)
            .where(ForecastMemory.event_time <= trading_session.evaluation_window_end)
        )
        or 0
    )


def _excluded_trade_count(
    session: Session,
    *,
    trading_session: TradingSession,
    cutoff: datetime,
) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(TradeMemory)
            .where(TradeMemory.recorded_at > cutoff)
            .where(TradeMemory.event_time >= trading_session.evaluation_window_start)
            .where(TradeMemory.event_time <= trading_session.evaluation_window_end)
        )
        or 0
    )


def _latest_by_key(rows: list[Any], attr: str) -> list[Any]:
    latest: dict[str, Any] = {}
    for row in rows:
        key = str(getattr(row, attr))
        current = latest.get(key)
        if current is None or _row_sort_key(row) >= _row_sort_key(current):
            latest[key] = row
    return [latest[key] for key in sorted(latest)]


def _manifest(
    *,
    trading_session: TradingSession,
    evaluation_as_of: datetime,
    data_mode: str,
    market_rows: list[MarketMemory],
    forecast_events: list[ForecastMemory],
    trade_events: list[TradeMemory],
    excluded: dict[str, int],
) -> dict[str, Any]:
    refs = []
    refs.extend(_row_fingerprint("market_memory", row.market_memory_id, row) for row in market_rows)
    refs.extend(
        _row_fingerprint("forecast_memory", row.forecast_memory_event_id, row)
        for row in forecast_events
    )
    refs.extend(
        _row_fingerprint("trade_memory", row.trade_memory_event_id, row) for row in trade_events
    )
    input_checksum = checksum_payload(refs)
    watermarks = {
        "market_memory": _max_recorded_at(market_rows),
        "forecast_memory": _max_recorded_at(forecast_events),
        "trade_memory": _max_recorded_at(trade_events),
    }
    manifest_body = {
        "phase_3o_dataset_id": stable_phase_3p_id(
            "dataset",
            trading_session.trading_session_id,
            evaluation_as_of.isoformat(),
            data_mode,
            input_checksum,
        ),
        "trading_session_id": trading_session.trading_session_id,
        "evaluation_as_of": evaluation_as_of.isoformat(),
        "data_mode": data_mode,
        "source_max_recorded_at": max(
            (value for value in watermarks.values() if value),
            default=None,
        ),
        "source_row_counts": {
            "market_memory": len(market_rows),
            "forecast_memory": len(forecast_events),
            "trade_memory": len(trade_events),
        },
        "source_partition_watermarks": watermarks,
        "excluded_rows_by_reason": excluded,
        "input_checksum": input_checksum,
    }
    manifest = {"generated_at": utc_now().isoformat(), **manifest_body}
    manifest["manifest_hash"] = checksum_payload(manifest_body)
    return manifest


def _row_fingerprint(store: str, row_id: str, row: Any) -> dict[str, Any]:
    return {
        "store": store,
        "id": row_id,
        "event_sequence": int(getattr(row, "event_sequence", 0) or 0),
        "recorded_at": _stable_datetime_text(getattr(row, "recorded_at", None)),
        "payload_hash": getattr(row, "payload_hash", None),
        "is_correction": int(getattr(row, "is_correction", 0) or 0),
    }


def _max_recorded_at(rows: list[Any]) -> str | None:
    values = [row.recorded_at for row in rows if row.recorded_at is not None]
    return _stable_datetime_text(max(values)) if values else None


def _row_sort_key(row: Any) -> tuple[int, datetime]:
    return int(row.event_sequence or 0), row.recorded_at


def _source_ref(store: str, row_id: str) -> dict[str, str]:
    return {"reference_type": "PHASE_3O_SOURCE", "store": store, "reference_id": row_id}


def _require_datetime(value: datetime | str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("evaluation_as_of is required.")
    return parsed


def _after(value: datetime, cutoff: datetime) -> bool:
    if value.tzinfo is None and cutoff.tzinfo is not None:
        return value > cutoff.replace(tzinfo=None)
    if value.tzinfo is not None and cutoff.tzinfo is None:
        return value.replace(tzinfo=None) > cutoff
    return value > cutoff


def _stable_datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat()
