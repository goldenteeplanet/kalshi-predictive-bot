from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy.orm import Session

from kalshi_predictor.backtesting.reports import generate_backtest_report
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.ingestion import ingest_crypto_quotes
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.repository import parse_symbols
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.jobs.collect_once import collect_once
from kalshi_predictor.leaderboard.reports import generate_leaderboard_report
from kalshi_predictor.opportunities.reports import (
    generate_market_rankings_report,
    generate_opportunities_report,
)
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.overnight.health import run_health_checks
from kalshi_predictor.overnight.repository import (
    collect_iteration_metrics,
    complete_overnight_cycle,
    create_overnight_cycle,
)
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.paper.reports import write_paper_trading_report
from kalshi_predictor.paper.simulator import run_paper_trading
from kalshi_predictor.tournament.reports import generate_tournament_report
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.linker import link_weather_markets

StepJob = Callable[[Session, Settings], Mapping[str, Any]]


@dataclass(frozen=True)
class OvernightCycleResult:
    run_id: int
    cycle_id: int
    cycle_number: int
    status: str
    markets_collected: int
    snapshots_inserted: int
    forecasts_inserted: int
    paper_orders_created: int
    opportunities_detected: int
    settlements_synced: int
    reports_generated: int
    errors: list[dict[str, Any]]
    summary: dict[str, Any]


@dataclass
class OvernightJobs:
    collect_markets: StepJob = field(default_factory=lambda: _default_collect_markets)
    ingest_crypto: StepJob = field(default_factory=lambda: _default_ingest_crypto)
    build_crypto_features: StepJob = field(default_factory=lambda: _default_build_crypto_features)
    link_crypto_markets: StepJob = field(default_factory=lambda: _default_link_crypto_markets)
    ingest_weather: StepJob = field(default_factory=lambda: _default_ingest_weather)
    build_weather_features: StepJob = field(default_factory=lambda: _default_build_weather_features)
    link_weather_markets: StepJob = field(default_factory=lambda: _default_link_weather_markets)
    forecast_all: StepJob = field(default_factory=lambda: _default_forecast_all)
    update_model_weights: StepJob = field(default_factory=lambda: _default_update_model_weights)
    forecast_target_model: StepJob = field(default_factory=lambda: _default_forecast_target_model)
    find_opportunities: StepJob = field(default_factory=lambda: _default_find_opportunities)
    paper_run: StepJob = field(default_factory=lambda: _default_paper_run)
    paper_pnl: StepJob = field(default_factory=lambda: _default_paper_pnl)
    sync_settlements: StepJob = field(default_factory=lambda: _default_sync_settlements)
    backtest: StepJob = field(default_factory=lambda: _default_backtest)
    reports: StepJob = field(default_factory=lambda: _default_reports)
    health: Callable[[Session, Settings], dict[str, Any]] = field(
        default_factory=lambda: _default_health
    )


def run_overnight_cycle(
    session: Session,
    *,
    run_id: int,
    cycle_number: int,
    settings: Settings | None = None,
    jobs: OvernightJobs | None = None,
) -> OvernightCycleResult:
    resolved_settings = settings or get_settings()
    resolved_jobs = jobs or OvernightJobs()
    cycle = create_overnight_cycle(session, run_id=run_id, cycle_number=cycle_number)
    errors: list[dict[str, Any]] = []
    steps: dict[str, Any] = {}

    health = resolved_jobs.health(session, resolved_settings)
    steps["health"] = health
    if not health.get("ok", False):
        for check in health.get("errors", []):
            errors.append({"step": "health", "error": check.get("detail"), "check": check})
        summary = _summary(
            steps=steps,
            errors=errors,
            settings=resolved_settings,
            demo_message=_demo_message(resolved_settings),
        )
        return _finish_cycle(session, cycle=cycle, status="ERROR", summary=summary, errors=errors)

    job_order: list[tuple[str, StepJob]] = [
        ("collect_markets", resolved_jobs.collect_markets),
        ("ingest_crypto", resolved_jobs.ingest_crypto),
        ("build_crypto_features", resolved_jobs.build_crypto_features),
        ("link_crypto_markets", resolved_jobs.link_crypto_markets),
        ("ingest_weather", resolved_jobs.ingest_weather),
        ("build_weather_features", resolved_jobs.build_weather_features),
        ("link_weather_markets", resolved_jobs.link_weather_markets),
        ("forecast_all", resolved_jobs.forecast_all),
        ("update_model_weights", resolved_jobs.update_model_weights),
        ("forecast_target_model", resolved_jobs.forecast_target_model),
        ("find_opportunities", resolved_jobs.find_opportunities),
        ("paper_run", resolved_jobs.paper_run),
        ("paper_pnl", resolved_jobs.paper_pnl),
        ("sync_settlements", resolved_jobs.sync_settlements),
        ("backtest", resolved_jobs.backtest),
        ("reports", resolved_jobs.reports),
    ]
    for name, job in job_order:
        steps[name] = _run_step(
            name,
            job,
            session,
            resolved_settings,
            errors,
            stop_on_error=resolved_settings.overnight_stop_on_error,
        )
        if errors and resolved_settings.overnight_stop_on_error:
            break

    metric = collect_iteration_metrics(
        session,
        cycle_number=cycle_number,
        model_name=resolved_settings.overnight_model,
        raw={"steps": steps, "errors": errors},
        notes=_metric_notes(errors),
    )
    steps["model_iteration_metrics"] = {
        "id": metric.id,
        "forecast_count": metric.forecast_count,
        "opportunity_count": metric.opportunity_count,
        "paper_trade_count": metric.paper_trade_count,
        "estimated_pnl": metric.estimated_pnl,
        "realized_pnl": metric.realized_pnl,
        "avg_edge": metric.avg_edge,
        "avg_opportunity_score": metric.avg_opportunity_score,
    }
    status = "COMPLETED_WITH_ERRORS" if errors else "COMPLETED"
    summary = _summary(
        steps=steps,
        errors=errors,
        settings=resolved_settings,
        demo_message=_demo_message(resolved_settings),
    )
    return _finish_cycle(session, cycle=cycle, status=status, summary=summary, errors=errors)


def _run_step(
    name: str,
    job: StepJob,
    session: Session,
    settings: Settings,
    errors: list[dict[str, Any]],
    *,
    stop_on_error: bool,
) -> dict[str, Any]:
    try:
        return dict(job(session, settings))
    except Exception as exc:
        error = {"step": name, "type": type(exc).__name__, "error": str(exc)}
        errors.append(error)
        if stop_on_error:
            return {"status": "failed", "error": str(exc)}
        return {"status": "failed", "error": str(exc)}


def _finish_cycle(
    session: Session,
    *,
    cycle: Any,
    status: str,
    summary: dict[str, Any],
    errors: list[dict[str, Any]],
) -> OvernightCycleResult:
    counts = _counts(summary["steps"])
    complete_overnight_cycle(
        session,
        cycle,
        status=status,
        markets_collected=counts.markets_collected,
        snapshots_inserted=counts.snapshots_inserted,
        forecasts_inserted=counts.forecasts_inserted,
        paper_orders_created=counts.paper_orders_created,
        opportunities_detected=counts.opportunities_detected,
        settlements_synced=counts.settlements_synced,
        reports_generated=counts.reports_generated,
        errors=errors,
        summary=summary,
    )
    return OvernightCycleResult(
        run_id=cycle.overnight_run_id,
        cycle_id=cycle.id,
        cycle_number=cycle.cycle_number,
        status=status,
        markets_collected=counts.markets_collected,
        snapshots_inserted=counts.snapshots_inserted,
        forecasts_inserted=counts.forecasts_inserted,
        paper_orders_created=counts.paper_orders_created,
        opportunities_detected=counts.opportunities_detected,
        settlements_synced=counts.settlements_synced,
        reports_generated=counts.reports_generated,
        errors=errors,
        summary=summary,
    )


@dataclass(frozen=True)
class _CycleCounts:
    markets_collected: int = 0
    snapshots_inserted: int = 0
    forecasts_inserted: int = 0
    paper_orders_created: int = 0
    opportunities_detected: int = 0
    settlements_synced: int = 0
    reports_generated: int = 0


def _counts(steps: Mapping[str, Any]) -> _CycleCounts:
    collect_step = _step(steps, "collect_markets")
    forecast_all = _step(steps, "forecast_all")
    forecast_target = _step(steps, "forecast_target_model")
    opportunities = _step(steps, "find_opportunities")
    paper_run = _step(steps, "paper_run")
    settlements = _step(steps, "sync_settlements")
    reports = _step(steps, "reports")
    return _CycleCounts(
        markets_collected=_int(collect_step.get("markets_seen")),
        snapshots_inserted=_int(collect_step.get("snapshots_inserted")),
        forecasts_inserted=_int(collect_step.get("forecasts_inserted"))
        + _int(forecast_all.get("forecasts_inserted"))
        + _int(forecast_target.get("forecasts_inserted")),
        paper_orders_created=_int(paper_run.get("orders_created")),
        opportunities_detected=_int(opportunities.get("opportunities_detected")),
        settlements_synced=_int(settlements.get("settlements_synced")),
        reports_generated=_int(reports.get("reports_generated")),
    )


def _summary(
    *,
    steps: Mapping[str, Any],
    errors: list[dict[str, Any]],
    settings: Settings,
    demo_message: str,
) -> dict[str, Any]:
    counts = _counts(steps)
    return {
        "steps": dict(steps),
        "errors": errors,
        "counts": counts.__dict__,
        "model": settings.overnight_model,
        "paper_betting": "enabled" if settings.overnight_run_paper else "disabled",
        "demo_execution": demo_message,
        "next_learning_step": (
            "Compare model_iteration_metrics, leaderboard, tournament weights, and paper P&L "
            "after settlements sync."
        ),
    }


def _step(steps: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = steps.get(name)
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metric_notes(errors: list[dict[str, Any]]) -> str:
    if errors:
        return "Cycle completed with stored errors; compare paper P&L after fixing failed steps."
    return "Cycle completed; use paper P&L and settlements to refine model weights."


def _demo_message(settings: Settings) -> str:
    if not settings.overnight_run_demo:
        return "OVERNIGHT_RUN_DEMO=false; no demo orders are submitted."
    return "OVERNIGHT_RUN_DEMO=true was requested, but this loop still avoids demo execution."


def _default_health(session: Session, settings: Settings) -> dict[str, Any]:
    return run_health_checks(session, settings=settings)


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


def _default_ingest_crypto(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = ingest_crypto_quotes(session, symbols=parse_symbols(DEFAULT_CRYPTO_SYMBOLS))
    return {
        "source": summary.source,
        "prices_inserted": summary.prices_inserted,
        "errors": summary.errors,
    }


def _default_build_crypto_features(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = build_crypto_features(session, symbols=parse_symbols(DEFAULT_CRYPTO_SYMBOLS))
    return {
        "symbols_processed": summary.symbols_processed,
        "features_inserted": summary.features_inserted,
    }


def _default_link_crypto_markets(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = link_crypto_markets(session)
    return {
        "markets_scanned": summary.markets_scanned,
        "links_created": summary.links_created,
        "btc_links": summary.btc_links,
        "eth_links": summary.eth_links,
        "generic_links": summary.generic_links,
        "multi_asset_links": summary.multi_asset_links,
        "links_by_symbol": summary.links_by_symbol,
    }


def _default_ingest_weather(session: Session, settings: Settings) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": (
            "No overnight weather latitude/longitude setting exists yet; use ingest-weather "
            "with --input-file or --lat/--lon to seed forecasts."
        ),
        "location_key": settings.weather_v2_default_location_key,
    }


def _default_build_weather_features(session: Session, settings: Settings) -> dict[str, Any]:
    summary = build_weather_features(session, location_key=settings.weather_v2_default_location_key)
    return {
        "location_key": summary.location_key,
        "forecasts_processed": summary.forecasts_processed,
        "features_inserted": summary.features_inserted,
    }


def _default_link_weather_markets(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = link_weather_markets(session)
    return {
        "markets_scanned": summary.markets_scanned,
        "links_created": summary.links_created,
        "by_metric": summary.by_metric,
        "by_location_key": summary.by_location_key,
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


def _default_update_model_weights(session: Session, settings: Settings) -> dict[str, Any]:
    path, result = generate_tournament_report(
        session,
        days=30,
        output_path=Path("reports/model_tournament.md"),
        name="overnight_model_tournament",
        generate_weights=True,
    )
    return {
        "report_path": str(path),
        "rows": len(result.rows),
        "weights_generated": len(result.weights),
    }


def _default_forecast_target_model(session: Session, settings: Settings) -> dict[str, Any]:
    snapshots = get_recent_snapshots(session, limit=100)
    summary = run_forecast_models(session, model_name=settings.overnight_model, snapshots=snapshots)
    return {
        "model_name": settings.overnight_model,
        "snapshots_scanned": summary.snapshots_scanned,
        "forecasts_inserted": summary.forecasts_inserted,
        "skipped": summary.skipped,
    }


def _default_find_opportunities(session: Session, settings: Settings) -> dict[str, Any]:
    summary = scan_opportunities(
        session,
        model_name=settings.overnight_model,
        limit=settings.opportunity_max_results,
        settings=settings,
    )
    return {
        "markets_scanned": summary.markets_scanned,
        "rankings_inserted": summary.rankings_inserted,
        "opportunities_detected": summary.opportunities_detected,
        "top_opportunity_ticker": summary.top_opportunity_ticker,
        "top_opportunity_score": str(summary.top_opportunity_score or ""),
    }


def _default_paper_run(session: Session, settings: Settings) -> dict[str, Any]:
    if not settings.overnight_run_paper:
        return {"status": "skipped", "orders_created": 0, "reason": "OVERNIGHT_RUN_PAPER=false"}
    summary = run_paper_trading(session, settings=settings, model_name=settings.overnight_model)
    return {
        "forecasts_scanned": summary.forecasts_scanned,
        "decisions_generated": summary.decisions_generated,
        "orders_created": summary.orders_created,
        "fills_created": summary.fills_created,
        "skipped_due_to_edge": summary.skipped_due_to_edge,
        "skipped_due_to_risk_limits": summary.skipped_due_to_risk_limits,
        "duplicates_skipped": summary.duplicates_skipped,
    }


def _default_paper_pnl(session: Session, settings: Settings) -> dict[str, Any]:
    if not settings.overnight_run_paper:
        return {"status": "skipped", "pnl_rows_inserted": 0}
    summary = calculate_and_store_pnl(session)
    return {
        "positions_evaluated": summary.positions_evaluated,
        "pnl_rows_inserted": summary.pnl_rows_inserted,
        "realized_pnl": str(summary.realized_pnl),
        "unrealized_pnl": str(summary.unrealized_pnl),
        "total_pnl": str(summary.total_pnl),
    }


def _default_sync_settlements(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    count = sync_settlements(
        lookback_days=30,
        limit=100,
        max_pages=1,
        session=session,
    )
    return {"settlements_synced": count}


def _default_backtest(session: Session, settings: Settings) -> dict[str, Any]:
    if not settings.overnight_run_backtest:
        return {"status": "skipped", "reason": "OVERNIGHT_RUN_BACKTEST=false"}
    path = generate_backtest_report(
        session,
        model_name=settings.overnight_model,
        strategy_name="paper_v1",
        days=30,
        output_path=Path("reports/backtest_overnight.md"),
    )
    return {"report_path": str(path)}


def _default_reports(session: Session, settings: Settings) -> dict[str, Any]:
    if not settings.overnight_run_reports:
        return {"status": "skipped", "reports_generated": 0, "paths": []}
    paths: list[str] = []
    path, _summary = generate_opportunities_report(
        session,
        model_name=settings.overnight_model,
        limit=settings.opportunity_max_results,
        output_path=Path("reports/opportunities.md"),
        settings=settings,
    )
    paths.append(str(path))
    ranking_report = generate_market_rankings_report(
        session,
        limit=50,
        output_path=Path("reports/market_rankings.md"),
    )
    paths.append(str(ranking_report))
    path, _leaderboard = generate_leaderboard_report(
        session,
        days=30,
        output_path=Path("reports/model_leaderboard.md"),
    )
    paths.append(str(path))
    path, _tournament = generate_tournament_report(
        session,
        days=30,
        output_path=Path("reports/model_tournament.md"),
        name="overnight_report_tournament",
        generate_weights=True,
    )
    paths.append(str(path))
    paper_report = write_paper_trading_report(
        session,
        Path("reports/paper_trading.md"),
        settings=settings,
    )
    paths.append(str(paper_report))
    return {"reports_generated": len(paths), "paths": paths}
