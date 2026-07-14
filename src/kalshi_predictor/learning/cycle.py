from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.confidence.engine import run_model_confidence_engine
from kalshi_predictor.confidence.reports import generate_model_confidence_report
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.data.schema import PaperOrder
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.jobs.collect_once import collect_once
from kalshi_predictor.lanes.metrics import refresh_learning_metrics
from kalshi_predictor.lanes.repository import (
    insert_learning_opportunity,
    insert_learning_trade_for_order,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.learning.reports import (
    generate_learning_report,
    generate_learning_targets_report,
)
from kalshi_predictor.learning.repository import (
    complete_learning_cycle,
    create_learning_cycle,
)
from kalshi_predictor.learning.safety import settled_paper_trade_count
from kalshi_predictor.learning.targets import generate_learning_targets
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.paper.simulator import run_paper_trading

StepJob = Callable[[Session, Settings], Mapping[str, Any]]


@dataclass(frozen=True)
class LearningCycleResult:
    run_id: int
    cycle_id: int
    cycle_number: int
    status: str
    markets_scanned: int
    forecasts_generated: int
    opportunities_found: int
    paper_trades_created: int
    settlements_synced: int
    settled_paper_trades_total: int
    errors: list[dict[str, Any]]
    summary: dict[str, Any]


@dataclass
class LearningJobs:
    collect_markets: StepJob = field(default_factory=lambda: _default_collect_markets)
    forecast_all: StepJob = field(default_factory=lambda: _default_forecast_all)
    sync_settlements: StepJob = field(default_factory=lambda: _default_sync_settlements)
    update_confidence: StepJob = field(default_factory=lambda: _default_update_confidence)
    generate_targets: StepJob = field(default_factory=lambda: _default_generate_targets)
    find_opportunities: StepJob = field(default_factory=lambda: _default_find_opportunities)
    paper_run: StepJob = field(default_factory=lambda: _default_paper_run)
    paper_pnl: StepJob = field(default_factory=lambda: _default_paper_pnl)
    reports: StepJob = field(default_factory=lambda: _default_reports)


def run_learning_cycle(
    session: Session,
    *,
    run_id: int,
    cycle_number: int,
    settings: Settings | None = None,
    jobs: LearningJobs | None = None,
) -> LearningCycleResult:
    base_settings = settings or get_settings()
    resolved_settings = learning_paper_settings(base_settings)
    resolved_jobs = jobs or LearningJobs()
    cycle = create_learning_cycle(session, run_id=run_id, cycle_number=cycle_number)
    errors: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}

    job_order: list[tuple[str, StepJob]] = [
        ("collect_markets", resolved_jobs.collect_markets),
        ("forecast_all", resolved_jobs.forecast_all),
        ("sync_settlements", resolved_jobs.sync_settlements),
        ("update_confidence", resolved_jobs.update_confidence),
        ("generate_targets", resolved_jobs.generate_targets),
        ("find_opportunities", resolved_jobs.find_opportunities),
        ("paper_run", resolved_jobs.paper_run),
        ("paper_pnl", resolved_jobs.paper_pnl),
        ("reports", resolved_jobs.reports),
    ]
    for name, job in job_order:
        steps[name] = _run_step(name, job, session, resolved_settings, errors)

    counts = _counts(steps)
    status = "COMPLETED_WITH_ERRORS" if errors else "COMPLETED"
    summary = {
        "steps": steps,
        "errors": errors,
        "counts": counts.__dict__,
        "mode": "PAPER_ONLY_LEARNING",
        "model_name": resolved_settings.learning_model_name,
        "demo_execution": "blocked",
        "live_execution": "not available",
    }
    complete_learning_cycle(
        session,
        cycle,
        status=status,
        markets_scanned=counts.markets_scanned,
        forecasts_generated=counts.forecasts_generated,
        opportunities_found=counts.opportunities_found,
        paper_trades_created=counts.paper_trades_created,
        settlements_synced=counts.settlements_synced,
        errors=errors,
        summary=summary,
    )
    return LearningCycleResult(
        run_id=cycle.learning_run_id,
        cycle_id=cycle.id,
        cycle_number=cycle.cycle_number,
        status=status,
        markets_scanned=counts.markets_scanned,
        forecasts_generated=counts.forecasts_generated,
        opportunities_found=counts.opportunities_found,
        paper_trades_created=counts.paper_trades_created,
        settlements_synced=counts.settlements_synced,
        settled_paper_trades_total=settled_paper_trade_count(session),
        errors=errors,
        summary=summary,
    )


@dataclass(frozen=True)
class _CycleCounts:
    markets_scanned: int = 0
    forecasts_generated: int = 0
    opportunities_found: int = 0
    paper_trades_created: int = 0
    settlements_synced: int = 0


def _run_step(
    name: str,
    job: StepJob,
    session: Session,
    settings: Settings,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return dict(job(session, settings))
    except Exception as exc:
        error = {"step": name, "type": type(exc).__name__, "error": str(exc)}
        errors.append(error)
        return {"status": "failed", "error": str(exc)}


def _counts(steps: Mapping[str, Any]) -> _CycleCounts:
    collect_step = _step(steps, "collect_markets")
    forecast_step = _step(steps, "forecast_all")
    opportunities = _step(steps, "find_opportunities")
    paper_run = _step(steps, "paper_run")
    settlements = _step(steps, "sync_settlements")
    return _CycleCounts(
        markets_scanned=_int(collect_step.get("markets_seen"))
        or _int(opportunities.get("markets_scanned")),
        forecasts_generated=_int(collect_step.get("forecasts_inserted"))
        + _int(forecast_step.get("forecasts_inserted")),
        opportunities_found=_int(opportunities.get("opportunities_detected")),
        paper_trades_created=_int(paper_run.get("orders_created")),
        settlements_synced=_int(settlements.get("settlements_synced")),
    )


def _step(steps: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = steps.get(name)
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _quiet_console() -> Console:
    return Console(file=StringIO())


def _default_collect_markets(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = collect_once(
        status="open",
        limit=100,
        max_pages=1,
        include_orderbook=True,
        session=session,
        console=_quiet_console(),
    )
    return {
        "markets_seen": summary.markets_seen,
        "snapshots_inserted": summary.snapshots_inserted,
        "forecasts_inserted": summary.forecasts_inserted,
        "skipped_forecasts": summary.skipped_forecasts,
    }


def _default_forecast_all(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    snapshots = get_recent_snapshots(session, limit=100)
    summary = run_forecast_models(session, model_name="all", snapshots=snapshots)
    return {
        "snapshots_scanned": summary.snapshots_scanned,
        "forecasts_inserted": summary.forecasts_inserted,
        "skipped": summary.skipped,
    }


def _default_sync_settlements(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    count = sync_settlements(lookback_days=30, limit=100, max_pages=1, session=session)
    return {"settlements_synced": count}


def _default_update_confidence(session: Session, settings: Settings) -> dict[str, Any]:
    result = run_model_confidence_engine(session, settings=settings)
    return {
        "scores_inserted": result.scores_inserted,
        "weights_inserted": result.weights_inserted,
        "rows": len(result.rows),
    }


def _default_generate_targets(session: Session, settings: Settings) -> dict[str, Any]:
    result = generate_learning_targets(
        session,
        settings=settings,
        model_name=settings.learning_model_name,
        limit=100,
    )
    return {"targets_inserted": result.inserted, "targets_scanned": result.scanned}


def _default_find_opportunities(session: Session, settings: Settings) -> dict[str, Any]:
    summary = scan_opportunities(
        session,
        model_name=settings.learning_model_name,
        limit=max(100, settings.opportunity_max_results),
        settings=settings,
    )
    lane_rows = 0
    for opportunity in summary.opportunities:
        insert_learning_opportunity(
            session,
            {
                **opportunity,
                "source": "learning-cycle",
            },
        )
        lane_rows += 1
    return {
        "markets_scanned": summary.markets_scanned,
        "rankings_inserted": summary.rankings_inserted,
        "opportunities_detected": summary.opportunities_detected,
        "learning_opportunities_inserted": lane_rows,
        "top_opportunity_ticker": summary.top_opportunity_ticker,
    }


def _default_paper_run(session: Session, settings: Settings) -> dict[str, Any]:
    before_ids = {int(order_id) for order_id in session.scalars(select(PaperOrder.id))}
    summary = run_paper_trading(
        session,
        settings=settings,
        model_name=settings.learning_model_name,
    )
    new_orders = session.scalars(select(PaperOrder).order_by(PaperOrder.created_at, PaperOrder.id))
    lane_rows = 0
    for order in new_orders:
        if order.id is None or order.id in before_ids:
            continue
        insert_learning_trade_for_order(session, order, source="learning-cycle")
        lane_rows += 1
    metric = refresh_learning_metrics(session, settings=settings)
    return {
        "forecasts_scanned": summary.forecasts_scanned,
        "decisions_generated": summary.decisions_generated,
        "orders_created": summary.orders_created,
        "learning_paper_trades_inserted": lane_rows,
        "learning_metric_id": metric.id,
        "fills_created": summary.fills_created,
        "skipped_due_to_edge": summary.skipped_due_to_edge,
        "skipped_due_to_risk_limits": summary.skipped_due_to_risk_limits,
        "duplicates_skipped": summary.duplicates_skipped,
        "candidate_scan_limit": summary.candidate_scan_limit,
        "learning_candidates_scanned": summary.forecasts_scanned,
    }


def _default_paper_pnl(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = calculate_and_store_pnl(session)
    return {
        "positions_evaluated": summary.positions_evaluated,
        "pnl_rows_inserted": summary.pnl_rows_inserted,
        "realized_pnl": str(summary.realized_pnl),
        "unrealized_pnl": str(summary.unrealized_pnl),
        "total_pnl": str(summary.total_pnl),
    }


def _default_reports(session: Session, settings: Settings) -> dict[str, Any]:
    paths = [
        generate_learning_report(session, settings=settings),
        generate_learning_targets_report(session, settings=settings, refresh=False),
        generate_model_confidence_report(
            session,
            output_path=Path("reports/model_confidence.md"),
            settings=settings,
            refresh=False,
        ),
    ]
    return {"reports_generated": len(paths), "paths": [str(path) for path in paths]}
