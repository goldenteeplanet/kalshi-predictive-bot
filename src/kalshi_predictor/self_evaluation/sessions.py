from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.self_evaluation.contracts import TradingSession
from kalshi_predictor.utils.time import parse_datetime, utc_now


def resolve_trading_session(
    *,
    session_date: str | date | None = None,
    evaluation_as_of: datetime | str | None = None,
    settings: Settings | None = None,
) -> TradingSession:
    resolved = settings or get_settings()
    timezone_name = resolved.phase_3p_session_timezone
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid Phase 3P session timezone: {timezone_name}") from exc

    as_of = parse_datetime(evaluation_as_of) if evaluation_as_of is not None else utc_now()
    if as_of is None:
        raise ValueError("evaluation_as_of must be a valid datetime.")
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone)
    local_as_of = as_of.astimezone(timezone)

    resolved_date = _session_date(session_date, local_as_of)
    session_open = datetime.combine(resolved_date, time.min, tzinfo=timezone)
    session_close = datetime.combine(resolved_date, time.max, tzinfo=timezone)
    label = resolved_date.isoformat()
    return TradingSession(
        trading_session_id=f"session-{label}",
        calendar_id=resolved.phase_3p_session_calendar_id,
        session_label=label,
        session_timezone=timezone_name,
        session_open_at=session_open,
        session_close_at=session_close,
        evaluation_window_start=session_open,
        evaluation_window_end=session_close,
        is_holiday=False,
        is_early_close=False,
        includes_overnight_hours=False,
    )


def normalize_evaluation_as_of(
    value: datetime | str | None = None,
    *,
    settings: Settings | None = None,
) -> datetime:
    resolved = settings or get_settings()
    timezone = ZoneInfo(resolved.phase_3p_session_timezone)
    parsed = parse_datetime(value) if value is not None else utc_now()
    if parsed is None:
        raise ValueError("evaluation_as_of must be a valid datetime.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def _session_date(value: str | date | None, local_as_of: datetime) -> date:
    if value is None:
        return (local_as_of - timedelta(days=1)).date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("session_date must be YYYY-MM-DD.") from exc
