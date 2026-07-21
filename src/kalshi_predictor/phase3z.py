from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

import kalshi_predictor
from kalshi_predictor.active_universe import current_market_predicate
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    detect_backend,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.maintenance import database_health, migration_status, sqlite_backup
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    ForecastSkipLog,
    Market,
    MarketLeg,
    MarketOpportunity,
    MarketSnapshot,
    MicrostructureFeature,
    NewsFeature,
    NewsMarketLink,
    PaperOrder,
    Settlement,
    SportsFeature,
    SportsMarketLink,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.status import model_status_rows
from kalshi_predictor.market_legs import (
    DISPLAY_CATEGORIES,
    LINKED_CATEGORIES,
    link_coverage_dashboard,
    parse_and_store_market_legs,
)
from kalshi_predictor.utils.time import utc_now

MODEL_REPAIR_DIR = Path("reports/model_repair")
MARKET_COVERAGE_DIR = Path("reports/market_coverage")
PHASE3Z_VERSION = "phase3z_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"

ROLE_BY_MODEL = {
    "market_implied_v1": "BENCHMARK",
    "ensemble_v1": "SHADOW",
    "ensemble_v2": "PAPER_TRADING",
    "crypto_v2": "CANDIDATE",
    "weather_v2": "CANDIDATE",
    "economic_v1": "CANDIDATE",
    "news_v1": "CANDIDATE",
    "sports_v1": "CANDIDATE",
    "microstructure_v1": "CANDIDATE",
    "meta_v1": "SHADOW",
}

FEATURE_TABLES = {
    "crypto_v2": CryptoFeature,
    "weather_v2": WeatherFeature,
    "economic_v1": EconomicFeature,
    "news_v1": NewsFeature,
    "sports_v1": SportsFeature,
    "microstructure_v1": MicrostructureFeature,
}

LINK_TABLES = {
    "crypto_v2": CryptoMarketLink,
    "weather_v2": WeatherMarketLink,
    "economic_v1": EconomicMarketLink,
    "news_v1": NewsMarketLink,
    "sports_v1": SportsMarketLink,
}


@dataclass(frozen=True)
class Phase3ZArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    extra_paths: tuple[Path, ...] = ()


def runtime_identity(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    db_url = database_url_from_settings(resolved)
    db_location = describe_db_location(db_url)
    root = _repo_root()
    cwd = Path.cwd().resolve()
    python_executable = Path(sys.executable).resolve()
    package_path = Path(kalshi_predictor.__file__).resolve()
    sqlite_path = sqlite_path_from_url(db_url)
    sqlite_identity = _sqlite_identity(sqlite_path) if sqlite_path else None
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3Z",
        "phase_version": PHASE3Z_VERSION,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "repository_root": str(root),
        "current_working_directory": str(cwd),
        "runtime_path_warning": _runtime_path_warning(
            repo_root=root,
            cwd=cwd,
            python_executable=python_executable,
            package_path=package_path,
        ),
        "git_commit": _git_value(root, "rev-parse", "HEAD"),
        "git_branch": _git_value(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "python_executable": str(python_executable),
        "package_path": str(package_path),
        "database_backend": detect_backend(resolved, db_url=db_url),
        "database_url": redact_database_url(db_url),
        "database_location": db_location,
        "sqlite": sqlite_identity,
        "migration": migration_status(session=session, settings=resolved, db_url=db_url),
        "health": database_health(session=session, settings=resolved, db_url=db_url),
        "split_brain": _split_brain_status(db_url),
    }


def _fast_runtime_identity(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    del session
    resolved = settings or get_settings()
    db_url = database_url_from_settings(resolved)
    root = _repo_root()
    cwd = Path.cwd().resolve()
    python_executable = Path(sys.executable).resolve()
    package_path = Path(kalshi_predictor.__file__).resolve()
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3Z",
        "phase_version": PHASE3Z_VERSION,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "refresh_mode": "FAST_BOUNDED",
        "repository_root": str(root),
        "current_working_directory": str(cwd),
        "runtime_path_warning": _runtime_path_warning(
            repo_root=root,
            cwd=cwd,
            python_executable=python_executable,
            package_path=package_path,
        ),
        "git_commit": _git_value(root, "rev-parse", "HEAD"),
        "git_branch": _git_value(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "python_executable": str(python_executable),
        "package_path": str(package_path),
        "database_backend": detect_backend(resolved, db_url=db_url),
        "database_url": redact_database_url(db_url),
        "database_location": describe_db_location(db_url),
        "sqlite": None,
        "migration": {"status": "SKIPPED_FAST_REFRESH"},
        "health": {"status": "SKIPPED_FAST_REFRESH"},
        "split_brain": None,
    }


def build_model_repair_audit(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    identity = runtime_identity(session, settings=settings)
    readiness = model_status_rows(session)
    rows = [_model_audit_row(session, row) for row in readiness]
    paper_totals = _paper_trade_totals(session)
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["health_state"]] = status_counts.get(row["health_state"], 0) + 1
    return {
        "runtime_identity": identity,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "status_counts": status_counts,
        "paper_trade_reconciliation": paper_totals,
        "models": rows,
        "recommendations": _model_repair_recommendations(rows, paper_totals),
    }


def write_model_repair_audit(
    session: Session,
    *,
    output_dir: Path = MODEL_REPAIR_DIR,
    settings: Settings | None = None,
) -> Phase3ZArtifactSet:
    audit = build_model_repair_audit(session, settings=settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "model_repair_audit.json"
    md_path = output_dir / "model_repair_audit.md"
    status_path = output_dir / "model_status.json"
    paper_path = output_dir / "paper_trade_reconciliation.json"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")
    status_path.write_text(
        json.dumps({"models": audit["models"]}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    paper_path.write_text(
        json.dumps(audit["paper_trade_reconciliation"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_render_model_repair_markdown(audit), encoding="utf-8")
    return Phase3ZArtifactSet(output_dir, json_path, md_path, (status_path, paper_path))


def build_market_coverage_doctor(
    session: Session,
    *,
    settings: Settings | None = None,
    parse_first: bool = True,
    parse_limit: int | None = None,
    deep_checks: bool = True,
) -> dict[str, Any]:
    identity = (
        runtime_identity(session, settings=settings)
        if deep_checks
        else _fast_runtime_identity(session, settings=settings)
    )
    parse_result = (
        parse_and_store_market_legs(session, limit=parse_limit, refresh=False)
        if parse_first
        else None
    )
    dashboard = link_coverage_dashboard(session)
    stage_counts = _market_coverage_stage_counts(
        session,
        dashboard,
        parse_result=parse_result,
        deep_checks=deep_checks,
    )
    rows = [_coverage_contract_row(row, stage_counts) for row in dashboard["category_rows"]]
    collapse = _first_zero_stage(stage_counts)
    return {
        "runtime_identity": identity,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "refresh_mode": "DEEP" if deep_checks else "FAST_BOUNDED",
        "bounded_refresh": {
            "parse_first": parse_result is not None,
            "parse_limit": parse_limit,
            "deep_checks": deep_checks,
            "orphan_link_check": (
                "COMPLETED" if deep_checks else "SKIPPED_FAST_REFRESH"
            ),
            "detail_exports": "BOUNDED_EXAMPLES",
        },
        "parse_result": _parse_result_payload(parse_result),
        "stage_counts": stage_counts,
        "coverage_rows": rows,
        "dashboard": dashboard,
        "first_collapse": collapse,
        "recommendations": _coverage_recommendations(rows, collapse),
    }


def write_market_coverage_doctor(
    session: Session,
    *,
    output_dir: Path = MARKET_COVERAGE_DIR,
    settings: Settings | None = None,
    parse_first: bool = True,
    parse_limit: int | None = None,
    deep_checks: bool = True,
) -> Phase3ZArtifactSet:
    payload = build_market_coverage_doctor(
        session,
        settings=settings,
        parse_first=parse_first,
        parse_limit=parse_limit,
        deep_checks=deep_checks,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "market_coverage_doctor.json"
    md_path = output_dir / "market_coverage_doctor.md"
    rows_path = output_dir / "coverage_rows.json"
    link_path = output_dir / "link_coverage.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    rows_path.write_text(
        json.dumps(payload["coverage_rows"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    link_path.write_text(
        json.dumps(payload["dashboard"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_render_coverage_doctor_markdown(payload), encoding="utf-8")
    return Phase3ZArtifactSet(output_dir, json_path, md_path, (rows_path, link_path))


def write_model_metrics_reconcile(
    session: Session,
    *,
    output_dir: Path = MODEL_REPAIR_DIR,
    include_historical: bool = False,
    settings: Settings | None = None,
) -> Phase3ZArtifactSet:
    payload = {
        "runtime_identity": runtime_identity(session, settings=settings),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "include_historical": include_historical,
        "settlement_reconciliation": _settlement_reconciliation(session),
        "paper_trade_reconciliation": _paper_trade_totals(session),
        "note": (
            "This Phase 3Z command classifies local outcomes and paper trades only; "
            "it does not call authenticated live-order endpoints."
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "settlement_reconciliation.json"
    paper_path = output_dir / "paper_trade_reconciliation.json"
    md_path = output_dir / "metrics_reconciliation.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    paper_path.write_text(
        json.dumps(payload["paper_trade_reconciliation"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    md_path.write_text(_render_metrics_reconcile_markdown(payload), encoding="utf-8")
    return Phase3ZArtifactSet(output_dir, json_path, md_path, (paper_path,))


def write_model_repair_run(
    session: Session,
    *,
    output_dir: Path = MODEL_REPAIR_DIR,
    settings: Settings | None = None,
) -> Phase3ZArtifactSet:
    audit = write_model_repair_audit(session, output_dir=output_dir, settings=settings)
    coverage_dir = output_dir / "market_coverage"
    coverage = write_market_coverage_doctor(session, output_dir=coverage_dir, settings=settings)
    metrics = write_model_metrics_reconcile(session, output_dir=output_dir, settings=settings)
    golden_trace_path = output_dir / "golden_trace.json"
    golden_trace = _golden_trace_summary(session, settings=settings)
    golden_trace_path.write_text(
        json.dumps(golden_trace, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    combined_path = output_dir / "model_repair_run.json"
    combined = {
        "runtime_identity": runtime_identity(session, settings=settings),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "artifacts": {
            "model_repair_audit": str(audit.json_path),
            "market_coverage_doctor": str(coverage.json_path),
            "metrics_reconciliation": str(metrics.json_path),
            "golden_trace": str(golden_trace_path),
        },
    }
    combined_path.write_text(json.dumps(combined, indent=2, sort_keys=True), encoding="utf-8")
    return Phase3ZArtifactSet(
        output_dir,
        combined_path,
        audit.markdown_path,
        audit.extra_paths + coverage.extra_paths + metrics.extra_paths + (golden_trace_path,),
    )


def backup_before_phase3z_write(
    *,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path | None:
    db_url = database_url_from_settings(settings or get_settings())
    if sqlite_path_from_url(db_url) is None:
        return None
    backup_dir = output_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return sqlite_backup(output_path=backup_dir / f"phase3z_{_stamp()}.db", db_url=db_url)


def _model_audit_row(session: Session, readiness_row: dict[str, Any]) -> dict[str, Any]:
    model_name = readiness_row["model_name"]
    stored_names = tuple(readiness_row.get("stored_model_names") or (model_name,))
    role = ROLE_BY_MODEL.get(model_name, "CANDIDATE")
    forecast_metrics = _forecast_metrics(session, stored_names)
    paper_metrics = _paper_metrics(session, stored_names)
    skip_distribution = _skip_distribution(session, stored_names)
    opportunity_count = _opportunity_count(session, stored_names)
    link_count = _link_count(session, model_name)
    feature_count = _feature_count(session, model_name)
    health = _phase3z_health_state(
        readiness_row,
        role=role,
        forecast_metrics=forecast_metrics,
        paper_metrics=paper_metrics,
        link_count=link_count,
        feature_count=feature_count,
    )
    return {
        "model_name": model_name,
        "stored_model_names": list(stored_names),
        "role": role,
        "version": "v1",
        "enabled": readiness_row["registered"],
        "owner": "local_phase3_pipeline",
        "health_state": health,
        "legacy_readiness_status": readiness_row["status"],
        "forecast_count": readiness_row["forecast_count"],
        "latest_forecast_at": readiness_row["latest_forecast_at"],
        "feature_count": feature_count,
        "link_count": link_count,
        "opportunity_count": opportunity_count,
        "paper_trade_count": paper_metrics["total_count"],
        "paper_trade_metrics": paper_metrics,
        "forecast_metrics": forecast_metrics,
        "missing_data": readiness_row["missing_data"],
        "available_data": readiness_row["available_data"],
        "skip_distribution": skip_distribution,
        "downstream_connection_status": _downstream_status(role, paper_metrics, opportunity_count),
        "recommended_next_command": _phase3z_next_command(readiness_row, health, role),
    }


def _phase3z_health_state(
    readiness_row: dict[str, Any],
    *,
    role: str,
    forecast_metrics: dict[str, Any],
    paper_metrics: dict[str, Any],
    link_count: int | None,
    feature_count: int | None,
) -> str:
    if not readiness_row["registered"]:
        return "DISABLED"
    missing = set(readiness_row["missing_data"])
    if "market snapshot" in missing:
        return "NEEDS_RAW_MARKET_DATA"
    if any("feature" in item for item in missing):
        return "NEEDS_FEATURES"
    if any("link" in item for item in missing):
        return "NEEDS_MARKET_LINKS"
    if readiness_row["status"] == "READY_BUT_NO_MATCHING_MARKETS":
        return "READY_NO_MATCHING_MARKETS"
    if role == "BENCHMARK":
        return "BENCHMARK_ONLY"
    if role == "SHADOW":
        return "SHADOW_ONLY"
    if paper_metrics["open_count"] > 0 and paper_metrics["resolved_count"] == 0:
        return "WAITING_FOR_SETTLEMENT"
    if readiness_row["forecast_count"] > 0 and role == "PAPER_TRADING":
        if paper_metrics["total_count"] == 0:
            return "ACTIVE_NO_ELIGIBLE_OPPORTUNITIES"
        return "ACTIVE_HEALTHY" if paper_metrics["resolved_count"] > 0 else "WAITING_FOR_SETTLEMENT"
    if readiness_row["forecast_count"] > 0:
        if link_count == 0 or feature_count == 0:
            return "DEGRADED"
        return "ACTIVE_HEALTHY"
    return "DEGRADED"


def _forecast_metrics(session: Session, model_names: tuple[str, ...]) -> dict[str, Any]:
    rows = list(
        session.execute(
            select(Forecast.yes_probability, Settlement.yes_settlement_value, Settlement.result)
            .join(Settlement, Settlement.ticker == Forecast.ticker)
            .where(Forecast.model_name.in_(model_names))
        )
    )
    errors: list[Decimal] = []
    for probability, yes_value, result in rows:
        predicted = _decimal_or_none(probability)
        outcome = _settlement_outcome(yes_value, result)
        if predicted is None or outcome is None:
            continue
        if predicted < 0 or predicted > 1:
            continue
        errors.append((predicted - outcome) ** 2)
    evaluated = len(errors)
    unresolved = int(
        session.scalar(
            select(func.count())
            .select_from(Forecast)
            .outerjoin(Settlement, Settlement.ticker == Forecast.ticker)
            .where(Forecast.model_name.in_(model_names), Settlement.ticker.is_(None))
        )
        or 0
    )
    if evaluated == 0:
        brier = None
    else:
        brier = str(
            (sum(errors, Decimal("0")) / Decimal(evaluated)).quantize(Decimal("0.0001"))
        )
    return {
        "evaluated_count": evaluated,
        "unresolved_count": unresolved,
        "voided_count": 0,
        "brier_score": brier,
        "log_loss": None,
        "calibration_error": None,
        "undefined_reason": None if evaluated else "NO_EVALUATED_OUTCOMES",
        "metric_version": PHASE3Z_VERSION,
        "data_cutoff": utc_now().isoformat(),
    }


def _paper_metrics(session: Session, model_names: tuple[str, ...]) -> dict[str, Any]:
    orders = list(session.scalars(select(PaperOrder).where(PaperOrder.model_name.in_(model_names))))
    settlements = {
        row.ticker: row
        for row in session.scalars(
            select(Settlement).where(
                Settlement.ticker.in_([order.ticker for order in orders] or [""])
            )
        )
    }
    open_count = 0
    rejected_count = 0
    resolved_count = 0
    voided_count = 0
    orphaned_count = 0
    waiting_for_settlement_count = 0
    pnl = Decimal("0")
    exposure = Decimal("0")
    wins = 0
    for order in orders:
        if order.status in {"CANCELLED", "EXPIRED"}:
            rejected_count += 1
            continue
        if order.status == "OPEN":
            open_count += 1
        if order.forecast_id is None:
            orphaned_count += 1
        settlement = settlements.get(order.ticker)
        if settlement is None:
            if order.status == "FILLED":
                waiting_for_settlement_count += 1
            continue
        outcome = _settlement_outcome(settlement.yes_settlement_value, settlement.result)
        if outcome is None:
            voided_count += 1
            continue
        entry = _decimal_or_none(order.market_price) or _decimal_or_none(order.limit_price)
        if entry is None:
            orphaned_count += 1
            continue
        quantity = Decimal(order.quantity)
        exposure += entry * quantity
        side = order.side.upper()
        payout = outcome if side in {"BUY_YES", "SELL_NO"} else Decimal("1") - outcome
        trade_pnl = (payout - entry) * quantity
        pnl += trade_pnl
        resolved_count += 1
        if trade_pnl > 0:
            wins += 1
    if resolved_count == 0 or exposure == 0:
        roi = None
    else:
        roi = str((pnl / exposure).quantize(Decimal("0.0001")))
    if resolved_count == 0:
        win_rate = None
    else:
        win_rate = str((Decimal(wins) / Decimal(resolved_count)).quantize(Decimal("0.0001")))
    return {
        "total_count": len(orders),
        "open_count": open_count,
        "resolved_count": resolved_count,
        "waiting_for_settlement_count": waiting_for_settlement_count,
        "voided_count": voided_count,
        "orphaned_count": orphaned_count,
        "rejected_count": rejected_count,
        "realized_pnl": None if resolved_count == 0 else str(pnl.quantize(Decimal("0.0001"))),
        "capital_at_risk": None if exposure == 0 else str(exposure.quantize(Decimal("0.0001"))),
        "roi": roi,
        "win_rate": win_rate,
        "undefined_reason": None if resolved_count else "NO_RESOLVED_TRADES",
        "cost_model_version": "paper_midpoint_v1",
    }


def _paper_trade_totals(session: Session) -> dict[str, Any]:
    rows = [_paper_metrics(session, (model,)) for model in _paper_model_names(session)]
    totals = {
        "total_count": sum(row["total_count"] for row in rows),
        "open_count": sum(row["open_count"] for row in rows),
        "resolved_count": sum(row["resolved_count"] for row in rows),
        "waiting_for_settlement_count": sum(row["waiting_for_settlement_count"] for row in rows),
        "voided_count": sum(row["voided_count"] for row in rows),
        "orphaned_count": sum(row["orphaned_count"] for row in rows),
        "rejected_count": sum(row["rejected_count"] for row in rows),
    }
    totals["unclassified_count"] = max(
        0,
        totals["total_count"]
        - totals["open_count"]
        - totals["waiting_for_settlement_count"]
        - totals["resolved_count"]
        - totals["voided_count"]
        - totals["rejected_count"],
    )
    totals["classification_note"] = (
        "Open and orphaned are overlapping evidence dimensions; resolved/voided/rejected "
        "are mutually exclusive outcome buckets."
    )
    return totals


def _paper_model_names(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(PaperOrder.model_name).distinct().order_by(PaperOrder.model_name)
        )
    )


def _skip_distribution(session: Session, model_names: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ForecastSkipLog.reason, func.count(ForecastSkipLog.id))
        .where(ForecastSkipLog.model_name.in_(model_names))
        .group_by(ForecastSkipLog.reason)
        .order_by(desc(func.count(ForecastSkipLog.id)))
        .limit(10)
    )
    return [{"reason": reason, "count": int(count)} for reason, count in rows]


def _opportunity_count(session: Session, model_names: tuple[str, ...]) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(MarketOpportunity)
            .where(MarketOpportunity.model_name.in_(model_names))
        )
        or 0
    )


def _link_count(session: Session, model_name: str) -> int | None:
    table = LINK_TABLES.get(model_name)
    if table is None:
        return None
    return _count(session, table)


def _feature_count(session: Session, model_name: str) -> int | None:
    table = FEATURE_TABLES.get(model_name)
    if table is None:
        return None
    return _count(session, table)


def _count(session: Session, table: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _downstream_status(
    role: str,
    paper_metrics: dict[str, Any],
    opportunity_count: int,
) -> str:
    if role in {"BENCHMARK", "SHADOW", "CANDIDATE"}:
        return f"{role}_NO_PAPER_TRADE_EXPECTATION"
    if paper_metrics["total_count"] > 0:
        return "PAPER_LEDGER_CONNECTED"
    if opportunity_count > 0:
        return "OPPORTUNITIES_WITHOUT_PAPER_TRADES"
    return "NO_POSITIVE_EV_OR_RISK_APPROVED_DECISIONS"


def _phase3z_next_command(row: dict[str, Any], health: str, role: str) -> str:
    if health in {"NEEDS_RAW_MARKET_DATA", "NEEDS_MARKET_LINKS", "NEEDS_FEATURES"}:
        return " && ".join(row["next_commands"])
    if health == "WAITING_FOR_SETTLEMENT":
        return "kalshi-bot model-metrics-reconcile --include-historical"
    if role in {"BENCHMARK", "SHADOW"}:
        return "kalshi-bot model-repair-audit --output-dir reports/model_repair"
    return "kalshi-bot find-opportunities --model-name ensemble_v2 --limit 20"


def _parse_result_payload(parse_result: Any | None) -> dict[str, int] | None:
    if parse_result is None:
        return None
    return {
        "markets_scanned": parse_result.markets_scanned,
        "markets_with_legs": parse_result.markets_with_legs,
        "legs_inserted": parse_result.legs_inserted,
        "markets_skipped_existing": parse_result.markets_skipped_existing,
        "existing_markets_with_legs": getattr(parse_result, "existing_markets_with_legs", 0),
    }


def _market_coverage_stage_counts(
    session: Session,
    dashboard: dict[str, Any],
    *,
    parse_result: Any | None = None,
    deep_checks: bool = True,
) -> dict[str, Any]:
    market_count = _count(session, Market)
    active_eligible = int(
        session.scalar(
            select(func.count())
            .select_from(Market)
            .where(current_market_predicate(now=utc_now()))
        )
        or 0
    )
    parsed_markets = int(
        session.scalar(select(func.count(func.distinct(MarketLeg.ticker)))) or 0
    )
    parsed_legs = _count(session, MarketLeg)
    domain_mapped = int(
        session.scalar(
            select(func.count(func.distinct(MarketLeg.ticker))).where(
                MarketLeg.category.in_(DISPLAY_CATEGORIES),
                MarketLeg.category != "unknown",
            )
        )
        or 0
    )
    external_links = {
        "crypto": _count(session, CryptoMarketLink),
        "weather": _count(session, WeatherMarketLink),
        "economic": _count(session, EconomicMarketLink),
        "news": _count(session, NewsMarketLink),
        "sports": _count(session, SportsMarketLink),
    }
    parse_attempts = (
        parse_result.markets_scanned
        if parse_result is not None
        else parsed_markets
    )
    parse_failures = (
        max(
            0,
            parse_result.markets_scanned
            - parse_result.markets_with_legs
            - getattr(parse_result, "existing_markets_with_legs", 0),
        )
        if parse_result is not None
        else 0
    )
    sports_reconciliation = dashboard.get("reconciliation", {}).get("sports", {})
    return {
        "api_series_seen": None,
        "api_events_seen": None,
        "api_markets_seen": market_count,
        "catalog_series": _distinct_count(session, Market.series_ticker) if deep_checks else None,
        "catalog_events": _distinct_count(session, Market.event_ticker) if deep_checks else None,
        "catalog_markets": market_count,
        "active_eligible_markets": active_eligible,
        "metadata_complete_markets": int(
            session.scalar(
                select(func.count()).select_from(Market).where(Market.title.is_not(None))
            )
            or 0
        ),
        "parse_attempts": parse_attempts,
        "parsed_markets": parsed_markets,
        "parsed_legs": parsed_legs,
        "parse_failures": parse_failures,
        "domain_mapped_markets": domain_mapped,
        "external_links": external_links,
        "derived_links": int(
            sports_reconciliation.get("derived_usable_link_rows")
            or _link_count_by_label(
                dashboard,
                "sports Kalshi-event-derived usable link rows",
            )
        ),
        "derived_markets": int(sports_reconciliation.get("derived_usable_markets") or 0),
        "verified_schedule_links": int(
            sports_reconciliation.get("verified_schedule_link_rows") or 0
        ),
        "verified_schedule_markets": int(
            sports_reconciliation.get("verified_schedule_markets") or 0
        ),
        "partial_links": int(sports_reconciliation.get("partial_link_rows") or 0),
        "partial_markets": int(sports_reconciliation.get("unresolved_partial_markets") or 0),
        "partial_legs": int(sports_reconciliation.get("unresolved_partial_legs") or 0),
        "orphan_links": _orphan_link_count(session) if deep_checks else None,
        "orphan_link_check": "COMPLETED" if deep_checks else "SKIPPED_FAST_REFRESH",
        "coverage_rows": len(dashboard["category_rows"]),
        "refresh_mode": "DEEP" if deep_checks else "FAST_BOUNDED",
        "stage_note": (
            "market-coverage-doctor runs a paper-only market-leg parser before reporting; "
            "parse_failures exclude markets that already had parsed legs."
            if parse_result is not None
            else (
                "No parser pass was run for this report; parse_failures is measured as 0. "
                "Fast bounded refresh skips deep orphan-link checks."
                if not deep_checks
                else "No parser pass was run for this report; parse_failures is measured as 0."
            )
        ),
    }


def _coverage_contract_row(row: dict[str, Any], stage_counts: dict[str, Any]) -> dict[str, Any]:
    denominator = row["parsed_markets"]
    usable = row["linked_markets"]
    coverage = None if denominator == 0 else round(usable / denominator, 4)
    current_denominator = int(row.get("current_linkable_markets") or 0)
    current_usable = int(row.get("current_linked_markets") or 0)
    current_coverage = (
        None
        if current_denominator == 0
        else round(current_usable / current_denominator, 4)
    )
    health = _coverage_health(row, stage_counts)
    return {
        "scope_type": "internal_category",
        "scope_key": row["category"],
        "data_cutoff": utc_now().isoformat(),
        "raw_catalog_markets": stage_counts["catalog_markets"],
        "eligible_markets": stage_counts["active_eligible_markets"],
        "metadata_complete_markets": stage_counts["metadata_complete_markets"],
        "parsed_markets": row["parsed_markets"],
        "parsed_legs": row["parsed_legs"],
        "current_parsed_markets": int(row.get("current_parsed_markets") or 0),
        "current_parsed_legs": int(row.get("current_parsed_legs") or 0),
        "current_linked_markets": current_usable,
        "current_unlinked_markets": int(row.get("current_unlinked_markets") or 0),
        "historical_unlinked_markets": int(row.get("historical_unlinked_markets") or 0),
        "parse_failures": 0,
        "external_linked_markets": row["linked_markets"],
        "derived_markets": row["derived_markets"],
        "partial_markets": row["partial_markets"],
        "partial_legs": row.get("partial_legs", row["partial_markets"]),
        "partial_link_rows": row.get("partial_link_rows", row["partial_markets"]),
        "derived_usable_markets": row.get("derived_usable_markets", row["derived_markets"]),
        "derived_usable_link_rows": row.get(
            "derived_usable_link_rows",
            row["derived_markets"],
        ),
        "verified_schedule_markets": row.get("verified_schedule_markets", 0),
        "verified_schedule_link_rows": row.get("verified_schedule_link_rows", 0),
        "usable_markets": usable,
        "fresh_executable_markets": None,
        "coverage": coverage,
        "coverage_denominator": denominator,
        "current_coverage": current_coverage,
        "current_coverage_denominator": current_denominator,
        "health": health,
        "reason_codes": _coverage_reason_codes(row, health, stage_counts),
        "next_action": _coverage_next_action(row, health),
    }


def _coverage_health(row: dict[str, Any], stage_counts: dict[str, Any]) -> str:
    if stage_counts["catalog_markets"] == 0:
        return "NO_CATALOG_DATA"
    if int(row.get("current_parsed_markets", row["parsed_markets"])) == 0:
        return "NO_COMPATIBLE_ACTIVE_MARKETS" if row["category"] in LINKED_CATEGORIES else "HEALTHY"
    if int(row.get("current_linked_markets", row["linked_markets"])) == 0 and row[
        "category"
    ] in LINKED_CATEGORIES:
        return "LINKER_NOT_RUN"
    if int(row.get("current_unlinked_markets") or 0) > 0 or row["partial_markets"] > 0:
        return "LINKER_DEGRADED"
    return "HEALTHY"


def _coverage_reason_codes(
    row: dict[str, Any],
    health: str,
    stage_counts: dict[str, Any],
) -> list[str]:
    if health == "NO_CATALOG_DATA":
        return ["CATALOG_NOT_SYNCED"]
    if row["parsed_markets"] == 0:
        return ["NO_COMPATIBLE_ACTIVE_MARKETS"]
    if health == "LINKER_NOT_RUN":
        return ["NO_EXTERNAL_CANDIDATE"]
    if health == "LINKER_DEGRADED":
        if int(row.get("current_unlinked_markets") or 0) > 0:
            return ["CURRENT_MARKET_LINK_GAP"]
        return ["LEGACY_IDENTIFIER"]
    if isinstance(stage_counts.get("orphan_links"), int) and stage_counts["orphan_links"] > 0:
        return ["ORPHAN_MARKET_LINK"]
    return []


def _coverage_next_action(row: dict[str, Any], health: str) -> dict[str, str]:
    if health == "NO_CATALOG_DATA":
        return {
            "summary": "Run active market catalog sync",
            "command": "kalshi-bot collect-once --status open --limit 100 --max-pages 1",
        }
    if health == "NO_COMPATIBLE_ACTIVE_MARKETS":
        return {
            "summary": "No compatible active markets at cutoff",
            "command": "kalshi-bot collect-once",
        }
    if health == "LINKER_NOT_RUN":
        return {
            "summary": f"Run {row['category']} link repair",
            "command": "kalshi-bot link-remediate",
        }
    if health == "LINKER_DEGRADED":
        if int(row.get("current_unlinked_markets") or 0) > 0:
            return {
                "summary": "Run bounded current-market link repair",
                "command": "kalshi-bot db-writer-monitor --json",
            }
        return {
            "summary": "Review partial or legacy links",
            "command": "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage",
        }
    return {"summary": "No action required", "command": "kalshi-bot link-coverage"}


def _settlement_reconciliation(session: Session) -> dict[str, Any]:
    return {
        "settlement_rows": _count(session, Settlement),
        "markets_with_settlement_ts": int(
            session.scalar(
                select(func.count()).select_from(Market).where(Market.settlement_ts.is_not(None))
            )
            or 0
        ),
        "forecast_rows_with_outcome_join": int(
            session.scalar(
                select(func.count())
                .select_from(Forecast)
                .join(Settlement, Settlement.ticker == Forecast.ticker)
            )
            or 0
        ),
        "current_historical_boundary": "local database only; external historical fetch not run",
        "idempotency": "read-only classification; no payout rows are created",
    }


def _golden_trace_summary(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    del settings
    has_snapshot = _count(session, MarketSnapshot) > 0
    has_link = sum(_count(session, table) for table in LINK_TABLES.values()) > 0
    has_feature = sum(_count(session, table) for table in FEATURE_TABLES.values()) > 0
    has_forecast = _count(session, Forecast) > 0
    has_opportunity = _count(session, MarketOpportunity) > 0
    has_paper = _count(session, PaperOrder) > 0
    has_settlement = _count(session, Settlement) > 0
    paper_totals = _paper_trade_totals(session)
    steps = {
        "market_snapshot": has_snapshot,
        "domain_link": has_link,
        "point_in_time_feature": has_feature,
        "forecast": has_forecast,
        "opportunity": has_opportunity,
        "paper_trade": has_paper,
        "settled_market_outcome": has_settlement,
        "realized_metrics": paper_totals["resolved_count"] > 0,
        "dashboard_model_card": True,
    }
    return {
        "runtime_identity": {"generated_at": utc_now().isoformat()},
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "steps": steps,
        "status": "PASS" if all(steps.values()) else "INCOMPLETE",
        "missing_steps": [key for key, value in steps.items() if not value],
    }


def _model_repair_recommendations(
    rows: list[dict[str, Any]],
    paper_totals: dict[str, Any],
) -> list[str]:
    recommendations = []
    for row in rows:
        if row["health_state"] in {"NEEDS_MARKET_LINKS", "NEEDS_FEATURES", "NEEDS_RAW_MARKET_DATA"}:
            recommendations.append(f"{row['model_name']}: {row['recommended_next_command']}")
    if paper_totals["resolved_count"] == 0 and paper_totals["total_count"] > 0:
        recommendations.append(
            "Paper trades exist but no resolved trades are available; sync settlements."
        )
    return recommendations or ["No blocking model-repair recommendation at this cutoff."]


def _coverage_recommendations(rows: list[dict[str, Any]], collapse: dict[str, Any]) -> list[str]:
    degraded = [
        row
        for row in rows
        if row["health"] not in {"HEALTHY", "NO_COMPATIBLE_ACTIVE_MARKETS"}
    ]
    if degraded:
        return [f"{row['scope_key']}: {row['next_action']['summary']}" for row in degraded]
    if collapse:
        return [f"First zero stage: {collapse['stage']}"]
    return ["Coverage pipeline is producing measurable rows."]


def _first_zero_stage(stage_counts: dict[str, Any]) -> dict[str, Any]:
    for stage in (
        "catalog_markets",
        "metadata_complete_markets",
        "parse_attempts",
        "parsed_markets",
        "domain_mapped_markets",
        "coverage_rows",
    ):
        if stage_counts.get(stage) == 0:
            return {"stage": stage, "count": 0}
    return {}


def _render_model_repair_markdown(audit: dict[str, Any]) -> str:
    identity = audit["runtime_identity"]
    lines = [
        "# Phase 3Z Model Repair Audit",
        "",
        f"- Generated at: {identity['generated_at']}",
        f"- Safety: {audit['paper_only_safety']}",
        f"- Repository: {identity['repository_root']}",
        (
            f"- Git: {identity.get('git_branch') or 'unknown'} / "
            f"{identity.get('git_commit') or 'unknown'}"
        ),
        f"- Database: {identity['database_location']}",
        "",
        "## Model Status",
        "",
        "| Model | Role | Health | Forecasts | Trades | Evaluated | Brier | ROI | Next command |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in audit["models"]:
        lines.append(
            "| {model_name} | {role} | {health_state} | {forecast_count} | "
            "{paper_trade_count} | {evaluated} | {brier} | {roi} | `{next}` |".format(
                **row,
                evaluated=row["forecast_metrics"]["evaluated_count"],
                brier=_display(row["forecast_metrics"]["brier_score"]),
                roi=_display(row["paper_trade_metrics"]["roi"]),
                next=row["recommended_next_command"],
            )
        )
    lines.extend(["", "## Paper Trade Reconciliation", ""])
    for key, value in audit["paper_trade_reconciliation"].items():
        lines.append(f"- {key}: {_display(value)}")
    lines.extend(["", "## Recommendations", ""])
    for recommendation in audit["recommendations"]:
        lines.append(f"- {recommendation}")
    lines.append("")
    return "\n".join(lines)


def _render_coverage_doctor_markdown(payload: dict[str, Any]) -> str:
    identity = payload["runtime_identity"]
    lines = [
        "# Phase 3Z Market Coverage Doctor",
        "",
        f"- Generated at: {identity['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Database: {identity['database_location']}",
        "",
        "## Parser Pass",
        "",
    ]
    parse_result = payload.get("parse_result")
    if parse_result:
        for key, value in parse_result.items():
            lines.append(f"- {key}: {_display(value)}")
    else:
        lines.append("- parser_pass: skipped")
    lines.extend(
        [
            "",
        "## Stage Counts",
        "",
        ]
    )
    for key, value in payload["stage_counts"].items():
        lines.append(f"- {key}: {_display(value)}")
    lines.extend(
        [
            "",
            "## Coverage Rows",
            "",
            (
                "| Scope | Health | Parsed Markets | Parsed Legs | External | Derived Markets | "
                "Verified Markets | Partial Markets | Partial Link Rows | Coverage | Next action |"
            ),
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in payload["coverage_rows"]:
        coverage_label = "—" if row["coverage"] is None else f"{row['coverage']:.1%}"
        lines.append(
            f"| {row['scope_key']} | {row['health']} | {row['parsed_markets']} | "
            f"{row['parsed_legs']} | {row['external_linked_markets']} | "
            f"{row['derived_usable_markets']} | {row['verified_schedule_markets']} | "
            f"{row['partial_markets']} | {row['partial_link_rows']} | "
            f"{coverage_label} | `{row['next_action']['command']}` |"
        )
    lines.extend(["", "## Recommendations", ""])
    for recommendation in payload["recommendations"]:
        lines.append(f"- {recommendation}")
    lines.append("")
    return "\n".join(lines)


def _render_metrics_reconcile_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3Z Metrics Reconciliation",
        "",
        f"- Generated at: {payload['runtime_identity']['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Include historical: {payload['include_historical']}",
        "",
        "## Settlements",
        "",
    ]
    for key, value in payload["settlement_reconciliation"].items():
        lines.append(f"- {key}: {_display(value)}")
    lines.extend(["", "## Paper Trades", ""])
    for key, value in payload["paper_trade_reconciliation"].items():
        lines.append(f"- {key}: {_display(value)}")
    lines.append("")
    return "\n".join(lines)


def _sqlite_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None or str(path) == ":memory:":
        return None
    resolved = path.expanduser().resolve()
    exists = resolved.exists()
    payload: dict[str, Any] = {
        "path": str(resolved),
        "exists": exists,
        "in_synced_folder": _in_synced_folder(resolved),
    }
    if not exists:
        return payload
    stat = resolved.stat()
    payload.update(
        {
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            "sha256": _sha256_file(resolved),
            "integrity_check": _sqlite_integrity_check(resolved),
        }
    )
    return payload


def _split_brain_status(db_url: str) -> dict[str, Any]:
    location = describe_db_location(db_url)
    return {
        "status": "OK",
        "cli_database": location,
        "ui_database": location,
        "worker_database": location,
        "report_database": location,
        "message": "Single process configuration resolves one database URL in this runtime.",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_integrity_check(path: Path) -> str:
    try:
        with sqlite3.connect(path) as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        return f"ERROR: {exc}"


def _in_synced_folder(path: Path) -> bool:
    lowered = str(path).lower()
    return any(name in lowered for name in ("onedrive", "dropbox", "google drive", "icloud"))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _runtime_path_warning(
    *,
    repo_root: Path,
    cwd: Path,
    python_executable: Path,
    package_path: Path,
) -> str | None:
    warnings: list[str] = []
    if not _path_inside(cwd, repo_root):
        warnings.append(
            "Current working directory differs from the installed package root; "
            "activate or rebuild the venv for this checkout."
        )
    if any(_in_synced_folder(path) for path in (repo_root, python_executable, package_path)):
        warnings.append(
            "Runtime files are inside a synced folder; prefer a Linux-local checkout "
            "for overnight SQLite runs."
        )
    return " ".join(warnings) or None


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return result.stdout.strip() or None


def _distinct_count(session: Session, column: Any) -> int:
    return int(session.scalar(select(func.count(func.distinct(column)))) or 0)


def _orphan_link_count(session: Session) -> int:
    market_tickers = select(Market.ticker)
    total = 0
    for table in (
        CryptoMarketLink,
        WeatherMarketLink,
        EconomicMarketLink,
        NewsMarketLink,
        SportsMarketLink,
    ):
        total += int(
            session.scalar(
                select(func.count()).select_from(table).where(table.ticker.not_in(market_tickers))
            )
            or 0
        )
    return total


def _link_count_by_label(dashboard: dict[str, Any], label: str) -> int:
    for row in dashboard.get("link_counts", []):
        if row.get("label") == label:
            return int(row.get("value") or 0)
    return 0


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _settlement_outcome(yes_value: Any, result: Any) -> Decimal | None:
    value = _decimal_or_none(yes_value)
    if value is not None:
        return Decimal("1") if value >= Decimal("0.5") else Decimal("0")
    normalized = str(result or "").lower()
    if normalized in {"yes", "y", "1", "true"}:
        return Decimal("1")
    if normalized in {"no", "n", "0", "false"}:
        return Decimal("0")
    return None


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
