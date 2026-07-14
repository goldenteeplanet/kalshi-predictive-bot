import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import AutopilotCycle, AutopilotRun, PaperPnl, RiskEvent
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def autopilot_config_payload(settings: Settings) -> dict[str, Any]:
    return {
        "AUTOPILOT_ENABLED": settings.autopilot_enabled,
        "AUTOPILOT_DRY_RUN": settings.autopilot_dry_run,
        "AUTOPILOT_MODEL": settings.autopilot_model,
        "AUTOPILOT_INTERVAL_SECONDS": settings.autopilot_interval_seconds,
        "AUTOPILOT_MAX_CYCLES": settings.autopilot_max_cycles,
        "AUTOPILOT_MAX_ORDERS_PER_CYCLE": settings.autopilot_max_orders_per_cycle,
        "AUTOPILOT_MAX_DAILY_ORDERS": settings.autopilot_max_daily_orders,
        "AUTOPILOT_MIN_EDGE": decimal_to_str(settings.autopilot_min_edge),
        "AUTOPILOT_MIN_OPPORTUNITY_SCORE": decimal_to_str(
            settings.autopilot_min_opportunity_score
        ),
        "AUTOPILOT_STOP_ON_DRAWDOWN": settings.autopilot_stop_on_drawdown,
        "AUTOPILOT_MAX_DAILY_DRAWDOWN": decimal_to_str(settings.autopilot_max_daily_drawdown),
        "AUTOPILOT_MAX_OPEN_DEMO_ORDERS": settings.autopilot_max_open_demo_orders,
        "AUTOPILOT_REQUIRE_FRESH_DATA_MINUTES": (
            settings.autopilot_require_fresh_data_minutes
        ),
        "KALSHI_ENV": settings.kalshi_env,
        "EXECUTION_ENABLED": settings.execution_enabled,
        "EXECUTION_DRY_RUN": settings.execution_dry_run,
        "EXECUTION_KILL_SWITCH": settings.execution_kill_switch,
    }


def create_autopilot_run(session: Session, settings: Settings) -> AutopilotRun:
    run = AutopilotRun(
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        model_name=settings.autopilot_model,
        dry_run=int(settings.autopilot_dry_run),
        max_cycles=settings.autopilot_max_cycles,
        cycles_completed=0,
        orders_attempted=0,
        orders_submitted=0,
        orders_blocked=0,
        stop_reason=None,
        config_json=encode_json(autopilot_config_payload(settings)),
        summary_json=None,
        notes=None,
    )
    session.add(run)
    session.flush()
    return run


def complete_autopilot_run(
    session: Session,
    run: AutopilotRun,
    *,
    status: str,
    cycles_completed: int,
    orders_attempted: int,
    orders_submitted: int,
    orders_blocked: int,
    stop_reason: str | None,
    summary: Mapping[str, Any],
) -> AutopilotRun:
    run.status = status
    run.completed_at = utc_now()
    run.cycles_completed = cycles_completed
    run.orders_attempted = orders_attempted
    run.orders_submitted = orders_submitted
    run.orders_blocked = orders_blocked
    run.stop_reason = stop_reason
    run.summary_json = encode_json(dict(summary))
    session.add(run)
    session.flush()
    return run


def create_autopilot_cycle(
    session: Session,
    *,
    run_id: int,
    cycle_number: int,
    settings: Settings,
) -> AutopilotCycle:
    cycle = AutopilotCycle(
        autopilot_run_id=run_id,
        cycle_number=cycle_number,
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        model_name=settings.autopilot_model,
        opportunities_scanned=0,
        orders_attempted=0,
        orders_submitted=0,
        orders_blocked=0,
        stop_reason=None,
        summary_json=None,
        notes=None,
    )
    session.add(cycle)
    session.flush()
    return cycle


def complete_autopilot_cycle(
    session: Session,
    cycle: AutopilotCycle,
    *,
    status: str,
    opportunities_scanned: int,
    orders_attempted: int,
    orders_submitted: int,
    orders_blocked: int,
    stop_reason: str | None,
    summary: Mapping[str, Any],
) -> AutopilotCycle:
    cycle.status = status
    cycle.completed_at = utc_now()
    cycle.opportunities_scanned = opportunities_scanned
    cycle.orders_attempted = orders_attempted
    cycle.orders_submitted = orders_submitted
    cycle.orders_blocked = orders_blocked
    cycle.stop_reason = stop_reason
    cycle.summary_json = encode_json(dict(summary))
    session.add(cycle)
    session.flush()
    return cycle


def insert_risk_event(
    session: Session,
    *,
    guardrail_name: str,
    message: str,
    severity: str = "BLOCK",
    event_type: str = "AUTOPILOT_GUARDRAIL_BLOCK",
    ticker: str | None = None,
    model_name: str | None = None,
    raw: Mapping[str, Any] | None = None,
    autopilot_run_id: int | None = None,
    autopilot_cycle_id: int | None = None,
) -> RiskEvent:
    event = RiskEvent(
        created_at=utc_now(),
        event_type=event_type,
        severity=severity,
        ticker=ticker,
        model_name=model_name,
        guardrail_name=guardrail_name,
        message=message,
        raw_json=encode_json(dict(raw or {})),
        autopilot_run_id=autopilot_run_id,
        autopilot_cycle_id=autopilot_cycle_id,
    )
    session.add(event)
    session.flush()
    return event


def latest_autopilot_run(session: Session) -> AutopilotRun | None:
    return session.scalar(
        select(AutopilotRun).order_by(desc(AutopilotRun.started_at), desc(AutopilotRun.id)).limit(1)
    )


def latest_autopilot_cycle(session: Session) -> AutopilotCycle | None:
    return session.scalar(
        select(AutopilotCycle)
        .order_by(desc(AutopilotCycle.started_at), desc(AutopilotCycle.id))
        .limit(1)
    )


def recent_autopilot_cycles(session: Session, *, limit: int = 10) -> list[AutopilotCycle]:
    return list(
        session.scalars(
            select(AutopilotCycle)
            .order_by(desc(AutopilotCycle.started_at), desc(AutopilotCycle.id))
            .limit(limit)
        )
    )


def recent_risk_events(session: Session, *, limit: int = 20) -> list[RiskEvent]:
    return list(
        session.scalars(
            select(RiskEvent)
            .order_by(desc(RiskEvent.created_at), desc(RiskEvent.id))
            .limit(limit)
        )
    )


def count_daily_submitted_orders(session: Session, *, now: datetime | None = None) -> int:
    start = _start_of_day(now or utc_now())
    value = session.scalar(
        select(func.coalesce(func.sum(AutopilotCycle.orders_submitted), 0)).where(
            AutopilotCycle.started_at >= start
        )
    )
    return int(value or 0)


def count_open_demo_orders(session: Session) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(AutopilotCycle.orders_submitted), 0)).where(
            AutopilotCycle.status.in_(("SUBMITTED", "COMPLETED"))
        )
    )
    return int(value or 0)


def current_daily_pnl(session: Session, *, now: datetime | None = None) -> Decimal:
    start = _start_of_day(now or utc_now())
    rows = session.scalars(select(PaperPnl).where(PaperPnl.calculated_at >= start))
    total = Decimal("0")
    for row in rows:
        total += to_decimal(row.total_pnl) or Decimal("0")
    return total


def has_duplicate_attempt(
    session: Session,
    *,
    ticker: str,
    model_name: str,
    side: str,
    now: datetime | None = None,
) -> bool:
    start = _start_of_day(now or utc_now())
    cycles = session.scalars(
        select(AutopilotCycle)
        .where(AutopilotCycle.started_at >= start)
        .where(AutopilotCycle.summary_json.is_not(None))
        .order_by(desc(AutopilotCycle.started_at), desc(AutopilotCycle.id))
    )
    for cycle in cycles:
        summary = decode_json(cycle.summary_json)
        for attempt in _attempts_from_summary(summary):
            if (
                attempt.get("ticker") == ticker
                and attempt.get("model_name") == model_name
                and attempt.get("side") == side
            ):
                return True
    return False


def decode_summary(value: str | None) -> dict[str, Any]:
    return decode_json(value)


def row_to_dict(row: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        data[key] = value.isoformat() if isinstance(value, datetime) else value
    return data


def _attempts_from_summary(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for key in ("order_attempts", "submitted_orders", "dry_run_orders"):
        value = summary.get(key)
        if isinstance(value, list):
            attempts.extend(item for item in value if isinstance(item, dict))
    return attempts


def _start_of_day(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.replace(hour=0, minute=0, second=0, microsecond=0)


def pretty_json(value: Mapping[str, Any] | str | None) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return json.dumps(parsed, indent=2, sort_keys=True, default=str)
    return json.dumps(dict(value or {}), indent=2, sort_keys=True, default=str)
