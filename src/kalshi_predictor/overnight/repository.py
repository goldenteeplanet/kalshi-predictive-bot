import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    Forecast,
    MarketOpportunity,
    MarketRanking,
    ModelIterationMetric,
    OvernightCycle,
    OvernightRun,
    PaperOrder,
    PaperPnl,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now


def overnight_config_payload(settings: Settings) -> dict[str, Any]:
    return {
        "OVERNIGHT_ENABLED": settings.overnight_enabled,
        "OVERNIGHT_INTERVAL_MINUTES": settings.overnight_interval_minutes,
        "OVERNIGHT_MAX_CYCLES": settings.overnight_max_cycles,
        "OVERNIGHT_MODEL": settings.overnight_model,
        "OVERNIGHT_RUN_PAPER": settings.overnight_run_paper,
        "OVERNIGHT_RUN_DEMO": settings.overnight_run_demo,
        "OVERNIGHT_RUN_BACKTEST": settings.overnight_run_backtest,
        "OVERNIGHT_RUN_REPORTS": settings.overnight_run_reports,
        "OVERNIGHT_MIN_FREE_DISK_MB": settings.overnight_min_free_disk_mb,
        "OVERNIGHT_STOP_ON_ERROR": settings.overnight_stop_on_error,
        "OVERNIGHT_REQUIRE_MARKET_DATA": settings.overnight_require_market_data,
        "KALSHI_ENV": settings.kalshi_env,
        "EXECUTION_ENABLED": settings.execution_enabled,
        "EXECUTION_DRY_RUN": settings.execution_dry_run,
        "FORUM_CONSENSUS_ENABLED": settings.forum_consensus_enabled,
        "FORUM_CONSENSUS_MIN_WINNERS": settings.forum_consensus_min_winners,
        "FORUM_CONSENSUS_MIN_WIN_RATE": decimal_to_str(
            settings.forum_consensus_min_win_rate
        ),
        "FORUM_CONSENSUS_LONGSHOT_MAX_PRICE": decimal_to_str(
            settings.forum_consensus_longshot_max_price
        ),
    }


def create_overnight_run(session: Session, settings: Settings) -> OvernightRun:
    run = OvernightRun(
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        cycles_requested=settings.overnight_max_cycles,
        cycles_completed=0,
        errors_count=0,
        config_json=encode_json(overnight_config_payload(settings)),
        summary_json=None,
    )
    session.add(run)
    session.flush()
    return run


def complete_overnight_run(
    session: Session,
    run: OvernightRun,
    *,
    status: str,
    cycles_completed: int,
    errors_count: int,
    summary: Mapping[str, Any],
) -> OvernightRun:
    run.status = status
    run.completed_at = utc_now()
    run.cycles_completed = cycles_completed
    run.errors_count = errors_count
    run.summary_json = encode_json(dict(summary))
    session.add(run)
    session.flush()
    return run


def create_overnight_cycle(
    session: Session,
    *,
    run_id: int,
    cycle_number: int,
) -> OvernightCycle:
    cycle = OvernightCycle(
        overnight_run_id=run_id,
        cycle_number=cycle_number,
        started_at=utc_now(),
        completed_at=None,
        status="RUNNING",
        markets_collected=0,
        snapshots_inserted=0,
        forecasts_inserted=0,
        paper_orders_created=0,
        opportunities_detected=0,
        settlements_synced=0,
        reports_generated=0,
        errors_json=None,
        summary_json=None,
    )
    session.add(cycle)
    session.flush()
    return cycle


def complete_overnight_cycle(
    session: Session,
    cycle: OvernightCycle,
    *,
    status: str,
    markets_collected: int,
    snapshots_inserted: int,
    forecasts_inserted: int,
    paper_orders_created: int,
    opportunities_detected: int,
    settlements_synced: int,
    reports_generated: int,
    errors: list[dict[str, Any]],
    summary: Mapping[str, Any],
) -> OvernightCycle:
    cycle.status = status
    cycle.completed_at = utc_now()
    cycle.markets_collected = markets_collected
    cycle.snapshots_inserted = snapshots_inserted
    cycle.forecasts_inserted = forecasts_inserted
    cycle.paper_orders_created = paper_orders_created
    cycle.opportunities_detected = opportunities_detected
    cycle.settlements_synced = settlements_synced
    cycle.reports_generated = reports_generated
    cycle.errors_json = encode_json(errors)
    cycle.summary_json = encode_json(dict(summary))
    session.add(cycle)
    session.flush()
    return cycle


def insert_model_iteration_metric(
    session: Session,
    *,
    cycle_number: int,
    model_name: str,
    forecast_count: int,
    opportunity_count: int,
    paper_trade_count: int,
    estimated_pnl: Decimal | None,
    realized_pnl: Decimal | None,
    avg_edge: Decimal | None,
    avg_opportunity_score: Decimal | None,
    notes: str,
    raw: Mapping[str, Any],
) -> ModelIterationMetric:
    metric = ModelIterationMetric(
        generated_at=utc_now(),
        cycle_number=cycle_number,
        model_name=model_name,
        forecast_count=forecast_count,
        opportunity_count=opportunity_count,
        paper_trade_count=paper_trade_count,
        estimated_pnl=decimal_to_str(estimated_pnl),
        realized_pnl=decimal_to_str(realized_pnl),
        avg_edge=decimal_to_str(avg_edge),
        avg_opportunity_score=decimal_to_str(avg_opportunity_score),
        notes=notes,
        raw_json=encode_json(dict(raw)),
    )
    session.add(metric)
    session.flush()
    return metric


def collect_iteration_metrics(
    session: Session,
    *,
    cycle_number: int,
    model_name: str,
    raw: Mapping[str, Any],
    notes: str,
) -> ModelIterationMetric:
    forecast_count = int(
        session.scalar(
            select(func.count()).select_from(Forecast).where(Forecast.model_name == model_name)
        )
        or 0
    )
    opportunity_count = int(
        session.scalar(
            select(func.count())
            .select_from(MarketOpportunity)
            .where(MarketOpportunity.model_name == model_name)
        )
        or 0
    )
    paper_trade_count = int(
        session.scalar(
            select(func.count()).select_from(PaperOrder).where(PaperOrder.model_name == model_name)
        )
        or 0
    )
    latest_pnl = session.scalar(
        select(PaperPnl).order_by(desc(PaperPnl.calculated_at), desc(PaperPnl.id)).limit(1)
    )
    realized_pnl = to_decimal(latest_pnl.realized_pnl) if latest_pnl is not None else None
    estimated_pnl = to_decimal(latest_pnl.total_pnl) if latest_pnl is not None else None
    avg_edge, avg_score = recent_ranking_averages(session, model_name=model_name)
    return insert_model_iteration_metric(
        session,
        cycle_number=cycle_number,
        model_name=model_name,
        forecast_count=forecast_count,
        opportunity_count=opportunity_count,
        paper_trade_count=paper_trade_count,
        estimated_pnl=estimated_pnl,
        realized_pnl=realized_pnl,
        avg_edge=avg_edge,
        avg_opportunity_score=avg_score,
        notes=notes,
        raw=raw,
    )


def recent_ranking_averages(
    session: Session,
    *,
    model_name: str,
    limit: int = 100,
) -> tuple[Decimal | None, Decimal | None]:
    rankings = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.forecast_model == model_name)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
            .limit(limit)
        )
    )
    return _average(rankings, "estimated_edge"), _average(rankings, "opportunity_score")


def latest_overnight_run(session: Session) -> OvernightRun | None:
    return session.scalar(
        select(OvernightRun).order_by(desc(OvernightRun.started_at), desc(OvernightRun.id)).limit(1)
    )


def latest_overnight_cycle(session: Session) -> OvernightCycle | None:
    return session.scalar(
        select(OvernightCycle)
        .order_by(desc(OvernightCycle.started_at), desc(OvernightCycle.id))
        .limit(1)
    )


def recent_overnight_cycles(session: Session, *, limit: int = 10) -> list[OvernightCycle]:
    return list(
        session.scalars(
            select(OvernightCycle)
            .order_by(desc(OvernightCycle.started_at), desc(OvernightCycle.id))
            .limit(limit)
        )
    )


def recent_iteration_metrics(
    session: Session,
    *,
    limit: int = 10,
) -> list[ModelIterationMetric]:
    return list(
        session.scalars(
            select(ModelIterationMetric)
            .order_by(desc(ModelIterationMetric.generated_at), desc(ModelIterationMetric.id))
            .limit(limit)
        )
    )


def decode_json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif key in {"summary_json", "errors_json", "config_json", "raw_json"}:
            value = decode_json_value(value)
        data[key] = value
    return data


def _average(rows: list[Any], attr: str) -> Decimal | None:
    values = [to_decimal(getattr(row, attr, None)) for row in rows]
    decimals = [value for value in values if value is not None]
    if not decimals:
        return None
    return sum(decimals, Decimal("0")) / Decimal(len(decimals))

