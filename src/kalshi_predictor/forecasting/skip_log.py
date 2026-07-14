from collections.abc import Mapping
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import ForecastSkipLog
from kalshi_predictor.utils.time import utc_now


def log_forecast_skip(
    session: Session,
    *,
    model_name: str,
    ticker: str,
    reason: str,
    required_data: Mapping[str, Any] | list[str] | str,
    available_data: Mapping[str, Any] | list[str] | str | None = None,
    raw_json: Mapping[str, Any] | None = None,
) -> ForecastSkipLog:
    record = ForecastSkipLog(
        model_name=model_name,
        ticker=ticker,
        skipped_at=utc_now(),
        reason=reason,
        required_data=encode_json(_jsonable(required_data)),
        available_data=encode_json(_jsonable(available_data or {})),
        raw_json=encode_json(dict(raw_json or {})),
    )
    session.add(record)
    session.flush()
    return record


def latest_skip_for_model(
    session: Session,
    model_name: str,
) -> ForecastSkipLog | None:
    return session.scalar(
        select(ForecastSkipLog)
        .where(ForecastSkipLog.model_name == model_name)
        .order_by(desc(ForecastSkipLog.skipped_at), desc(ForecastSkipLog.id))
        .limit(1)
    )


def skip_count_for_model(session: Session, model_name: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ForecastSkipLog)
            .where(ForecastSkipLog.model_name == model_name)
        )
        or 0
    )


def forecast_skip_row(row: ForecastSkipLog | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id,
        "model_name": row.model_name,
        "ticker": row.ticker,
        "skipped_at": row.skipped_at.isoformat(),
        "reason": row.reason,
        "required_data": decode_json(row.required_data),
        "available_data": decode_json(row.available_data),
        "raw_json": decode_json(row.raw_json),
    }


def _jsonable(value: Mapping[str, Any] | list[str] | str) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        return value
    return {"value": value}
