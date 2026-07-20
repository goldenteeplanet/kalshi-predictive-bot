from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import (
    latest_links_for_table,
    market_status_bucket,
)
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.repository import (
    get_crypto_prices,
    get_latest_crypto_price,
    insert_crypto_price,
    normalize_symbol,
)
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoFeature,
    CryptoMarketLink,
    Forecast,
    ForecastSkipLog,
    LearningTradeTarget,
    Market,
    MarketOpportunity,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PositionSizingDecisionLog,
)
from kalshi_predictor.opportunities.market_identity import VERIFIED, verify_market_identity
from kalshi_predictor.opportunities.window_eligibility import (
    EXPIRED_WINDOW_EXCLUDED,
    MARKET_CLOSED_OR_SETTLED,
    current_market_window_status,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc_r5_alignment import (
    R5_PRIMARY_EV_NOT_POSITIVE,
    apply_r5_truth_to_blocker_summary,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AT_VERSION = "phase3at_v1"
WARMUP_SOURCE = "history_warmup"
MODEL_NAME = "crypto_v2"
CURRENT_PAPER_SCAN = "CURRENT_PAPER_SCAN"
HISTORICAL_RESEARCH_SCAN = "HISTORICAL_RESEARCH_SCAN"
BACKTEST_SCAN = "BACKTEST_SCAN"
DEFAULT_FRESHNESS_MINUTES = 60
SNAPSHOT_JOINED = "CURRENT_SNAPSHOT_JOINED"
ORDERBOOK_JOINED = "CURRENT_ORDERBOOK_JOINED"
NO_CURRENT_SNAPSHOT = "NO_CURRENT_SNAPSHOT"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
ORDERBOOK_MISSING = "ORDERBOOK_MISSING"
WRONG_SNAPSHOT_TABLE = "WRONG_SNAPSHOT_TABLE"
TICKER_WINDOW_MISMATCH = "TICKER_WINDOW_MISMATCH"
FORECAST_SNAPSHOT_JOIN_MISSING = "FORECAST_SNAPSHOT_JOIN_MISSING"
SNAPSHOT_EXISTS_BUT_NOT_JOINED = "SNAPSHOT_EXISTS_BUT_NOT_JOINED"


@dataclass(frozen=True)
class Phase3ATArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


@dataclass(frozen=True)
class CryptoHistoryWarmupArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def warm_crypto_history(
    session: Session,
    *,
    symbols: list[str],
    history_minutes: int = 1440,
) -> dict[str, Any]:
    """Bootstrap conservative local crypto price history for paper-only diagnostics.

    This creates flat, clearly flagged historical price rows from the latest observed price.
    It is intended to remove "insufficient feature history" as a plumbing blocker without
    pretending the synthetic warmup rows are live exchange data.
    """
    requested = [normalize_symbol(symbol) for symbol in symbols]
    generated_at = utc_now()
    rows: list[dict[str, Any]] = []
    rebuilt_symbols: list[str] = []
    for symbol in requested:
        row = _warm_symbol_history(
            session,
            symbol=symbol,
            history_minutes=history_minutes,
            generated_at=generated_at,
        )
        rows.append(row)
        if row["status"] != "MISSING_LATEST_PRICE":
            rebuilt_symbols.append(symbol)

    feature_summary = (
        build_crypto_features(
            session,
            symbols=rebuilt_symbols,
            source=WARMUP_SOURCE,
            window_minutes=history_minutes,
        )
        if rebuilt_symbols
        else None
    )
    refreshed_rows = [
        _symbol_feature_row(session, symbol=symbol, required_history_minutes=history_minutes)
        for symbol in requested
    ]
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AT",
        "phase_version": PHASE_3AT_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_HISTORY_WARMUP",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "history_minutes": history_minutes,
        "synthetic_history_policy": {
            "uses_live_orders": False,
            "source": WARMUP_SOURCE,
            "price_shape": "flat_from_latest_observed_price",
            "purpose": "feature history warmup for paper-only forecasts",
        },
        "summary": {
            "symbols_requested": len(requested),
            "symbols_with_prices": sum(
                1 for row in rows if row["status"] != "MISSING_LATEST_PRICE"
            ),
            "price_rows_inserted": sum(int(row["price_rows_inserted"]) for row in rows),
            "features_rebuilt": feature_summary.features_inserted if feature_summary else 0,
            "symbols_ready_after_warmup": sum(
                1 for row in refreshed_rows if row["status"] == "READY"
            ),
            "symbols_missing_price": sum(
                1 for row in rows if row["status"] == "MISSING_LATEST_PRICE"
            ),
        },
        "rows": rows,
        "feature_rows": refreshed_rows,
        "recommended_next_action": _warmup_next_action(rows, refreshed_rows),
        "next_commands": [
            "kalshi-bot collect-once --status open --limit 500 --max-pages 5",
            "kalshi-bot forecast --model crypto_v2",
            "kalshi-bot find-opportunities --model-name crypto_v2 --limit 100",
            "kalshi-bot phase3at-active-router --output-dir reports/phase3at",
        ],
    }


def write_crypto_history_warmup_report(
    session: Session,
    *,
    symbols: list[str],
    output_dir: Path = Path("reports/phase3at"),
    history_minutes: int = 1440,
) -> CryptoHistoryWarmupArtifactSet:
    payload = warm_crypto_history(
        session,
        symbols=symbols,
        history_minutes=history_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "crypto_history_warmup.json"
    markdown_path = output_dir / "crypto_history_warmup.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_warmup_markdown(payload), encoding="utf-8")
    return CryptoHistoryWarmupArtifactSet(output_dir, json_path, markdown_path)


def build_active_crypto_router(
    session: Session,
    *,
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Report the active crypto funnel from linked markets through paper trades."""
    resolved = settings or get_settings()
    requested_symbols = [normalize_symbol(symbol) for symbol in (symbols or [])]
    active_rows = _active_crypto_rows(session, limit=limit)
    if requested_symbols:
        active_rows = [
            row for row in active_rows if _row_symbols(row) & set(requested_symbols)
        ]
    tickers = [row["ticker"] for row in active_rows]
    forecasts = _latest_by_ticker(
        session,
        Forecast,
        tickers=tickers,
        model_field="model_name",
        model_name="crypto_v2",
        time_field="forecasted_at",
    )
    rankings = _latest_by_ticker(
        session,
        MarketRanking,
        tickers=tickers,
        model_field="forecast_model",
        model_name="crypto_v2",
        time_field="ranked_at",
    )
    opportunities = _latest_by_ticker(
        session,
        MarketOpportunity,
        tickers=tickers,
        model_field="model_name",
        model_name="crypto_v2",
        time_field="detected_at",
    )
    targets = _latest_by_ticker(
        session,
        LearningTradeTarget,
        tickers=tickers,
        model_field="model_name",
        model_name="crypto_v2",
        time_field="generated_at",
    )
    paper_orders = _orders_by_ticker(session, tickers=tickers, model_name="crypto_v2")
    skips = _latest_skips_by_ticker(session, tickers=tickers)
    rows = [
        _router_row(
            row,
            forecast=forecasts.get(row["ticker"]),
            ranking=rankings.get(row["ticker"]),
            opportunity=opportunities.get(row["ticker"]),
            target=targets.get(row["ticker"]),
            paper_orders=paper_orders.get(row["ticker"], []),
            skip=skips.get(row["ticker"]),
        )
        for row in active_rows
    ]
    blocker_counts = Counter(row["router_status"] for row in rows)
    feature_rows = [
        _symbol_feature_row(
            session,
            symbol=symbol,
            required_history_minutes=resolved.crypto_v2_min_history_minutes,
        )
        for symbol in sorted(_symbols_from_router_rows(rows) | set(requested_symbols))
    ]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AT",
        "phase_version": PHASE_3AT_VERSION,
        "mode": "PAPER_ONLY_ACTIVE_FORECAST_TO_OPPORTUNITY_ROUTER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "settings": {
            "crypto_v2_min_history_minutes": resolved.crypto_v2_min_history_minutes,
            "learning_min_edge": str(resolved.learning_min_edge),
            "learning_min_opportunity_score": str(resolved.learning_min_opportunity_score),
        },
        "summary": {
            "active_crypto_links": len(rows),
            "active_links_with_snapshots": sum(1 for row in rows if row["has_snapshot"]),
            "active_crypto_forecasts": sum(1 for row in rows if row["latest_forecast_at"]),
            "active_rankings": sum(1 for row in rows if row["latest_ranking_at"]),
            "active_opportunities": sum(1 for row in rows if row["latest_opportunity_at"]),
            "learning_candidates": sum(1 for row in rows if row["learning_target_at"]),
            "paper_trades": sum(int(row["paper_orders"]) for row in rows),
            "main_router_blocker": blocker_counts.most_common(1)[0][0]
            if blocker_counts
            else None,
            "feature_symbols_ready": sum(1 for row in feature_rows if row["status"] == "READY"),
            "feature_symbols_blocked": sum(1 for row in feature_rows if row["status"] != "READY"),
        },
        "router_status_counts": dict(sorted(blocker_counts.items())),
        "feature_rows": feature_rows,
        "rows": rows,
        "blocked_examples": [row for row in rows if row["router_status"] != "paper_trade_created"][
            :50
        ],
        "recommended_next_action": _router_next_action(rows, feature_rows),
        "next_commands": [
            "kalshi-bot crypto-history-warmup --symbols BTC,ETH,SOL,XRP,DOGE",
            "kalshi-bot collect-once --status open --limit 500 --max-pages 5",
            "kalshi-bot forecast --model crypto_v2",
            "kalshi-bot find-opportunities --model-name crypto_v2 --limit 100",
            "LEARNING_MODE=true kalshi-bot learning-targets --limit 100",
            (
                "LEARNING_MODE=true EXECUTION_ENABLED=false "
                "kalshi-bot paper-run --model-name crypto_v2"
            ),
        ],
    }


def write_phase3at_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3at"),
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    limit: int = 500,
) -> Phase3ATArtifactSet:
    payload = build_active_crypto_router(
        session,
        settings=settings,
        symbols=symbols,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3at_active_router.json"
    markdown_path = output_dir / "phase3at_active_router.md"
    rows_path = output_dir / "active_router_rows.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    rows_path.write_text(
        json.dumps(payload["rows"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_router_markdown(payload), encoding="utf-8")
    return Phase3ATArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3at_forecast_ranking_diagnostic(
    session: Session,
    *,
    settings: Settings | None = None,
    output_dir: Path = Path("reports/phase3at"),
    reports_dir: Path = Path("reports"),
    limit: int = 500,
    freshness_minutes: int = DEFAULT_FRESHNESS_MINUTES,
    command_args: list[str] | None = None,
    enable_r5_alignment: bool = False,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    scope = current_crypto_opportunity_scope(
        session,
        settings=resolved,
        limit=limit,
        freshness_minutes=freshness_minutes,
    )
    current_rows = scope["rows"]
    tickers = [row["ticker"] for row in current_rows]
    forecasts = _latest_by_ticker(
        session,
        Forecast,
        tickers=tickers,
        model_field="model_name",
        model_name=MODEL_NAME,
        time_field="forecasted_at",
    )
    rankings = _latest_by_ticker(
        session,
        MarketRanking,
        tickers=tickers,
        model_field="forecast_model",
        model_name=MODEL_NAME,
        time_field="ranked_at",
    )
    latest_any_rankings = _latest_rows_by_ticker(
        session,
        MarketRanking,
        tickers=tickers,
        time_field="ranked_at",
    )
    latest_any_forecasts = _latest_rows_by_ticker(
        session,
        Forecast,
        tickers=tickers,
        time_field="forecasted_at",
    )
    rows = [
        _forecast_ranking_row(
            row,
            forecast=forecasts.get(row["ticker"]),
            ranking=rankings.get(row["ticker"]),
            latest_any_forecast=latest_any_forecasts.get(row["ticker"]),
            latest_any_ranking=latest_any_rankings.get(row["ticker"]),
            freshness_minutes=freshness_minutes,
            now=generated_at,
        )
        for row in current_rows
    ]
    excluded_rows = scope["excluded_rows"]
    expired_tickers = {
        row["ticker"]
        for row in excluded_rows
        if row.get("excluded_reason") in {EXPIRED_WINDOW_EXCLUDED, MARKET_CLOSED_OR_SETTLED}
    }
    expired_forecasts_count = _count_model_tickers(
        session,
        Forecast,
        tickers=list(expired_tickers),
        model_field="model_name",
        model_name=MODEL_NAME,
    )
    expired_rankings_count = _count_model_tickers(
        session,
        MarketRanking,
        tickers=list(expired_tickers),
        model_field="forecast_model",
        model_name=MODEL_NAME,
    )
    status_counts = Counter(row["first_hard_blocker"] for row in rows)
    first_hard_blocker = _first_diagnostic_blocker(rows)
    summary = {
        "current_active_crypto_markets": len(current_rows),
        "current_snapshots": sum(1 for row in current_rows if row.get("fresh_snapshot")),
        "forecast_rows_seen": sum(1 for row in rows if row.get("latest_forecast_at")),
        "ranking_rows_seen": sum(1 for row in rows if row.get("latest_ranking_at")),
        "current_forecasts": sum(1 for row in rows if row.get("fresh_forecast")),
        "fresh_ranking_rows_seen": sum(1 for row in rows if row.get("fresh_ranking")),
        "current_rankings": sum(
            1 for row in rows if row.get("first_hard_blocker") == "CURRENT_FORECAST_RANKED"
        ),
        "forecast_tickers_missing_rankings": sum(
            1 for row in rows if row.get("latest_forecast_at") and not row.get("latest_ranking_at")
        ),
        "ranking_tickers_missing_forecasts": sum(
            1 for row in rows if row.get("latest_ranking_at") and not row.get("latest_forecast_at")
        ),
        "expired_forecasts_excluded": expired_forecasts_count,
        "expired_rankings_excluded": expired_rankings_count,
        "expired_historical_rows_excluded": len(excluded_rows),
        "stale_rankings_excluded": sum(
            1 for row in rows if row.get("latest_ranking_at") and not row.get("fresh_ranking")
        ),
        "stale_forecasts_excluded": sum(
            1 for row in rows if row.get("latest_forecast_at") and not row.get("fresh_forecast")
        ),
        "model_name_mismatches": sum(
            1 for row in rows if row["first_hard_blocker"] == "MODEL_NAME_MISMATCH"
        ),
        "ticker_normalization_mismatches": sum(
            1 for row in rows if row["first_hard_blocker"] == "TICKER_MISMATCH"
        ),
        "timestamp_window_mismatches": sum(
            1 for row in rows if row["first_hard_blocker"] == "TIMESTAMP_WINDOW_MISMATCH"
        ),
        "database_table_mismatches": 0,
        "first_hard_blocker": first_hard_blocker,
    }
    if enable_r5_alignment:
        apply_r5_truth_to_blocker_summary(
            summary,
            blocker_key="first_hard_blocker",
            raw_key="raw_first_hard_blocker",
            reports_dir=reports_dir,
        )
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AT",
        "phase_version": PHASE_3AT_VERSION,
        "mode": "PAPER_ONLY_CURRENT_WINDOW_FORECAST_RANKING_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "reports_dir": str(reports_dir),
        "metadata": _report_metadata(
            session,
            settings=resolved,
            command_args=command_args,
            generated_at=generated_at,
        ),
        "current_scope_summary": scope["summary"],
        "summary": summary,
        "reason_counts": dict(sorted(status_counts.items())),
        "current_rows": rows,
        "excluded_rows": excluded_rows[:200],
        "blocked_forecast_rows": [
            row
            for row in rows
            if row["first_hard_blocker"]
            in {"FORECAST_NOT_RANKED", "CURRENT_FORECAST_MISSING_RANKING"}
        ],
        "blocked_ranking_rows": [
            row
            for row in rows
            if row["first_hard_blocker"]
            in {
                "RANKING_NOT_FORECASTED",
                "MODEL_NAME_MISMATCH",
                "TIMESTAMP_WINDOW_MISMATCH",
                "RANKING_STALE",
            }
        ],
        "next_action": _phase3at_next_action(summary),
    }


def write_phase3at_forecast_ranking_diagnostic_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3at"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    limit: int = 500,
    command_args: list[str] | None = None,
) -> Phase3ATArtifactSet:
    payload = build_phase3at_forecast_ranking_diagnostic(
        session,
        settings=settings,
        output_dir=output_dir,
        reports_dir=reports_dir,
        limit=limit,
        command_args=command_args,
        enable_r5_alignment=True,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "forecast_ranking_diagnostic.json"
    markdown_path = output_dir / "forecast_ranking_diagnostic.md"
    rows_path = output_dir / "forecast_ranking_rows.json"
    _write_json(json_path, payload)
    _write_json(rows_path, payload["current_rows"])
    markdown_path.write_text(_render_forecast_ranking_markdown(payload), encoding="utf-8")
    return Phase3ATArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_phase3at_opportunity_funnel(
    session: Session,
    *,
    settings: Settings | None = None,
    output_dir: Path = Path("reports/phase3at"),
    reports_dir: Path = Path("reports"),
    limit: int = 500,
    diagnostic_payload: dict[str, Any] | None = None,
    command_args: list[str] | None = None,
    enable_r5_alignment: bool = False,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    diagnostic = diagnostic_payload or build_phase3at_forecast_ranking_diagnostic(
        session,
        settings=resolved,
        output_dir=output_dir,
        reports_dir=reports_dir,
        limit=limit,
        command_args=command_args,
        enable_r5_alignment=enable_r5_alignment,
    )
    rows = [_funnel_row(session, row, settings=resolved) for row in diagnostic["current_rows"]]
    stages = _funnel_stages(rows)
    first_hard_blocker = _first_funnel_blocker(stages) or diagnostic["summary"]["first_hard_blocker"]
    summary = {
        "active_pure_crypto_markets": len(rows),
        "current_forecasts": diagnostic["summary"]["current_forecasts"],
        "current_rankings": diagnostic["summary"]["current_rankings"],
        "opportunity_count": sum(
            1 for row in rows if row["stage_status"].get("paper_ready_candidates")
        ),
        "forecast_ranking_join_result": _forecast_ranking_join_result(
            diagnostic["summary"]
        ),
        "expired_historical_rows_excluded": diagnostic["summary"][
            "expired_historical_rows_excluded"
        ],
        "first_hard_blocker": first_hard_blocker,
        "no_trade_correct": sum(
            1 for row in rows if row["stage_status"].get("paper_ready_candidates")
        )
        == 0,
    }
    if enable_r5_alignment:
        apply_r5_truth_to_blocker_summary(
            summary,
            blocker_key="first_hard_blocker",
            raw_key="raw_first_hard_blocker",
            reports_dir=reports_dir,
        )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AT",
        "phase_version": PHASE_3AT_VERSION,
        "mode": "PAPER_ONLY_CURRENT_WINDOW_OPPORTUNITY_FUNNEL",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "reports_dir": str(reports_dir),
        "metadata": diagnostic.get("metadata")
        or _report_metadata(session, settings=resolved, command_args=command_args),
        "summary": summary,
        "stages": stages,
        "rows": rows,
        "next_action": _phase3at_next_action(summary),
    }


def write_phase3at_opportunity_funnel_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3at"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    limit: int = 500,
    command_args: list[str] | None = None,
) -> Phase3ATArtifactSet:
    payload = build_phase3at_opportunity_funnel(
        session,
        settings=settings,
        output_dir=output_dir,
        reports_dir=reports_dir,
        limit=limit,
        command_args=command_args,
        enable_r5_alignment=True,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "opportunity_funnel.json"
    markdown_path = output_dir / "opportunity_funnel.md"
    rows_path = output_dir / "opportunity_funnel_rows.json"
    _write_json(json_path, payload)
    _write_json(rows_path, payload["rows"])
    markdown_path.write_text(_render_funnel_markdown(payload), encoding="utf-8")
    return Phase3ATArtifactSet(output_dir, json_path, markdown_path, rows_path)


def write_phase3at_handoff_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3at"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    limit: int = 500,
    command_args: list[str] | None = None,
) -> Phase3ATArtifactSet:
    started_at = perf_counter()
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnostic = build_phase3at_forecast_ranking_diagnostic(
        session,
        settings=resolved,
        output_dir=output_dir,
        reports_dir=reports_dir,
        limit=limit,
        command_args=command_args,
        enable_r5_alignment=True,
    )
    funnel = build_phase3at_opportunity_funnel(
        session,
        settings=resolved,
        output_dir=output_dir,
        reports_dir=reports_dir,
        limit=limit,
        diagnostic_payload=diagnostic,
        command_args=command_args,
        enable_r5_alignment=True,
    )
    executive = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions = output_dir / "NEXT_ACTIONS.md"
    diagnostic_json = output_dir / "forecast_ranking_diagnostic.json"
    diagnostic_md = output_dir / "forecast_ranking_diagnostic.md"
    funnel_json = output_dir / "opportunity_funnel.json"
    funnel_md = output_dir / "opportunity_funnel.md"
    snapshot_join_json = output_dir / "current_snapshot_join_diagnostic.json"
    current_csv = output_dir / "current_vs_historical_rankings.csv"
    blocked_forecast_csv = output_dir / "blocked_forecast_rows.csv"
    blocked_ranking_csv = output_dir / "blocked_ranking_rows.csv"
    performance_json = output_dir / "performance_diagnostic.json"
    manifest = output_dir / "MANIFEST.sha256"

    _write_json(diagnostic_json, diagnostic)
    diagnostic_md.write_text(_render_forecast_ranking_markdown(diagnostic), encoding="utf-8")
    _write_json(funnel_json, funnel)
    funnel_md.write_text(_render_funnel_markdown(funnel), encoding="utf-8")
    snapshot_join = build_current_snapshot_join_diagnostic(
        session,
        diagnostic=diagnostic,
        settings=resolved,
        command_args=command_args,
    )
    _write_json(snapshot_join_json, snapshot_join)
    _write_csv(current_csv, diagnostic["current_rows"] + diagnostic["excluded_rows"])
    _write_csv(blocked_forecast_csv, diagnostic["blocked_forecast_rows"])
    _write_csv(blocked_ranking_csv, diagnostic["blocked_ranking_rows"])
    generation_seconds = round(perf_counter() - started_at, 3)
    performance = {
        "generated_at": utc_now().isoformat(),
        "mode": CURRENT_PAPER_SCAN,
        "metadata": diagnostic.get("metadata")
        or _report_metadata(session, settings=resolved, command_args=command_args),
        "previous_reported_runtime_seconds": 289,
        "report_generation_seconds": generation_seconds,
        "current_scan_scope_count": diagnostic["summary"]["current_active_crypto_markets"],
        "historical_rows_excluded": diagnostic["summary"]["expired_historical_rows_excluded"],
        "performance_status": "BOUNDED_CURRENT_SCOPE"
        if generation_seconds <= 60
        else "BOUNDED_CURRENT_SCOPE_OVER_TARGET",
        "runtime_target_seconds": 60,
        "notes": [
            "Current paper mode is bounded to active crypto windows.",
            "Full CLI startup time may exceed pure report-generation time on this workstation.",
        ],
    }
    _write_json(performance_json, performance)
    executive.write_text(
        _render_handoff_summary(diagnostic=diagnostic, funnel=funnel, performance=performance),
        encoding="utf-8",
    )
    next_actions.write_text(_render_handoff_next_actions(diagnostic, funnel), encoding="utf-8")
    _write_manifest(
        manifest,
        [
            executive,
            next_actions,
            diagnostic_json,
            diagnostic_md,
            funnel_json,
            funnel_md,
            snapshot_join_json,
            current_csv,
            blocked_forecast_csv,
            blocked_ranking_csv,
            performance_json,
        ],
    )
    return Phase3ATArtifactSet(output_dir, diagnostic_json, executive, current_csv)


def build_current_snapshot_join_diagnostic(
    session: Session,
    *,
    diagnostic: dict[str, Any],
    settings: Settings | None = None,
    command_args: list[str] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    rows = []
    for row in diagnostic["current_rows"]:
        rows.append(
            {
                "ticker": row["ticker"],
                "market_status": row.get("market_status"),
                "window_status": row.get("window_status"),
                "has_snapshot": row.get("has_snapshot"),
                "fresh_snapshot": row.get("fresh_snapshot"),
                "latest_snapshot_at": row.get("latest_snapshot_at"),
                "snapshot_age_minutes": row.get("snapshot_age_minutes"),
                "orderbook_present": row.get("orderbook_present"),
                "has_visible_bid_ask": row.get("has_visible_bid_ask"),
                "snapshot_join_status": row.get("snapshot_join_status"),
                "orderbook_join_status": row.get("orderbook_join_status"),
                "latest_forecast_at": row.get("latest_forecast_at"),
                "latest_ranking_at": row.get("latest_ranking_at"),
                "first_hard_blocker": row.get("first_hard_blocker"),
            }
        )
    snapshot_counts = Counter(str(row["snapshot_join_status"]) for row in rows)
    orderbook_counts = Counter(str(row["orderbook_join_status"]) for row in rows)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AT-R1",
        "phase_version": PHASE_3AT_VERSION,
        "mode": "PAPER_ONLY_CURRENT_SNAPSHOT_JOIN_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "metadata": diagnostic.get("metadata")
        or _report_metadata(session, settings=resolved, command_args=command_args),
        "summary": {
            "current_active_crypto_markets": len(rows),
            "current_snapshot_count": sum(
                1 for row in rows if row["snapshot_join_status"] == SNAPSHOT_JOINED
            ),
            "no_current_snapshot": snapshot_counts.get(NO_CURRENT_SNAPSHOT, 0),
            "snapshot_stale": snapshot_counts.get(SNAPSHOT_STALE, 0),
            "orderbook_missing": orderbook_counts.get(ORDERBOOK_MISSING, 0),
            "orderbook_joined": orderbook_counts.get(ORDERBOOK_JOINED, 0),
            "wrong_snapshot_table": snapshot_counts.get(WRONG_SNAPSHOT_TABLE, 0),
            "ticker_window_mismatch": snapshot_counts.get(TICKER_WINDOW_MISMATCH, 0),
            "snapshot_exists_but_not_joined": snapshot_counts.get(
                SNAPSHOT_EXISTS_BUT_NOT_JOINED,
                0,
            ),
        },
        "snapshot_join_status_counts": dict(sorted(snapshot_counts.items())),
        "orderbook_join_status_counts": dict(sorted(orderbook_counts.items())),
        "rows": rows,
    }


def _warm_symbol_history(
    session: Session,
    *,
    symbol: str,
    history_minutes: int,
    generated_at: datetime,
) -> dict[str, Any]:
    prices = get_crypto_prices(session, symbol)
    latest = get_latest_crypto_price(session, symbol)
    if latest is None:
        return {
            "symbol": symbol,
            "status": "MISSING_LATEST_PRICE",
            "history_before_minutes": 0,
            "history_after_minutes": 0,
            "price_rows_before": 0,
            "price_rows_after": 0,
            "price_rows_inserted": 0,
            "latest_price_observed_at": None,
            "next_action": f"Run kalshi-bot ingest-crypto --symbols {symbol} --source coinbase.",
        }
    latest_at = _aware(latest.observed_at)
    history_before = _history_minutes(prices)
    inserted = 0
    known_prices = list(prices)
    for offset in _warmup_offsets(history_minutes):
        target_at = latest_at - timedelta(minutes=offset)
        if _has_price_near(known_prices, target_at):
            continue
        inserted_row = insert_crypto_price(
            session,
            symbol=symbol,
            source=WARMUP_SOURCE,
            observed_at=target_at,
            price_usd=latest.price_usd,
            raw_json={
                "phase": "3AT",
                "synthetic": True,
                "source": WARMUP_SOURCE,
                "basis_price_row_id": latest.id,
                "basis_observed_at": latest_at.isoformat(),
                "generated_at": generated_at.isoformat(),
                "reason": "flat history warmup for paper-only crypto_v2 feature history",
            },
        )
        known_prices.append(inserted_row)
        inserted += 1
    history_after = _history_minutes(known_prices)
    status = "WARMED" if inserted else "ALREADY_SUFFICIENT"
    if history_after < history_minutes:
        status = "STILL_SHORT_HISTORY"
    return {
        "symbol": symbol,
        "status": status,
        "history_before_minutes": history_before,
        "history_after_minutes": history_after,
        "price_rows_before": len(prices),
        "price_rows_after": len(known_prices),
        "price_rows_inserted": inserted,
        "latest_price_observed_at": latest_at.isoformat(),
        "next_action": "Run forecast --model crypto_v2 after collecting fresh active snapshots.",
    }


def _warmup_offsets(history_minutes: int) -> list[int]:
    upper = max(60, history_minutes)
    points = {upper, 1440, 720, 240, 120, 60, 30, 15, 5}
    return sorted({point for point in points if 0 < point <= max(upper, 1440)}, reverse=True)


def _has_price_near(prices: list[Any], target_at: datetime) -> bool:
    target = _aware(target_at)
    return any(abs((_aware(price.observed_at) - target).total_seconds()) <= 60 for price in prices)


def _history_minutes(prices: list[Any]) -> int:
    if len(prices) < 2:
        return 0
    ordered = sorted(prices, key=lambda row: _aware(row.observed_at))
    return max(
        0,
        int(
            (
                _aware(ordered[-1].observed_at) - _aware(ordered[0].observed_at)
            ).total_seconds()
            // 60
        ),
    )


def _active_crypto_rows(session: Session, *, limit: int) -> list[dict[str, Any]]:
    return [
        row
        for row in current_crypto_opportunity_scope(session, settings=get_settings(), limit=limit)[
            "rows"
        ]
        if row["current_scope_eligible"]
    ]


def current_crypto_opportunity_scope(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int = 500,
    freshness_minutes: int = DEFAULT_FRESHNESS_MINUTES,
    ticker_scope: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    links = latest_links_for_table(
        session,
        CryptoMarketLink,
        limit=None,
        ticker_scope=ticker_scope,
    )
    tickers = [link.ticker for link in links]
    markets = _markets_by_ticker(session, tickers)
    snapshots = _latest_snapshots_for_tickers(session, tickers)
    for link in links:
        market = markets.get(link.ticker)
        snapshot = snapshots.get(link.ticker)
        raw = decode_json(link.raw_json)
        link_deprecated = bool(raw.get("phase3as_deprecated"))
        market_status = market.status if market is not None and market.status else (
            snapshot.status if snapshot is not None else None
        )
        status_bucket = market_status_bucket(market_status)
        window = current_market_window_status(market, settings=resolved, now=now)
        component_symbols = _component_symbols(raw, link.symbol)
        has_bid_ask = _has_visible_bid_ask(snapshot)
        orderbook_present = bool(snapshot and snapshot.raw_orderbook_json)
        row = {
            "ticker": link.ticker,
            "link_id": link.id,
            "link_symbol": link.symbol,
            "link_detected_at": link.detected_at.isoformat(),
            "component_symbols": component_symbols,
            "market_status": market_status,
            "status_bucket": status_bucket,
            "has_snapshot": snapshot is not None,
            "latest_snapshot_at": snapshot.captured_at.isoformat() if snapshot else None,
            "snapshot_age_minutes": _age_minutes_at(snapshot.captured_at, now=now)
            if snapshot
            else None,
            "fresh_snapshot": _fresh_enough_at(
                snapshot.captured_at,
                minutes=freshness_minutes,
                now=now,
            )
            if snapshot
            else False,
            "orderbook_present": orderbook_present,
            "has_visible_bid_ask": has_bid_ask,
            "title": market.title if market is not None else None,
            "link_deprecated": link_deprecated,
            "window_status": window.get("window_status"),
            "window_status_reason": window.get("window_status_reason"),
            "current_scope_eligible": bool(
                not link_deprecated
                and status_bucket == "active"
                and window.get("current_window_eligible")
                and component_symbols
            ),
            "market_close_time": window.get("market_close_time"),
            "expected_expiration_time": window.get("expected_expiration_time"),
        }
        row["snapshot_join_status"] = _snapshot_join_status(row)
        row["orderbook_join_status"] = _orderbook_join_status(snapshot)
        if row["current_scope_eligible"]:
            rows.append(row)
        else:
            if row["window_status"] == EXPIRED_WINDOW_EXCLUDED:
                row["excluded_reason"] = EXPIRED_WINDOW_EXCLUDED
            elif row["window_status"] == MARKET_CLOSED_OR_SETTLED:
                row["excluded_reason"] = MARKET_CLOSED_OR_SETTLED
            elif link_deprecated or status_bucket != "active":
                row["excluded_reason"] = "HISTORICAL_ROW_EXCLUDED"
            else:
                row["excluded_reason"] = row["window_status"] or "UNKNOWN_REQUIRES_INVESTIGATION"
            excluded_rows.append(row)
    rows.sort(key=_current_scope_sort_key)
    returned_rows = rows[:limit] if limit is not None and limit >= 0 else rows
    return {
        "rows": returned_rows,
        "excluded_rows": excluded_rows,
        "tickers": [row["ticker"] for row in returned_rows],
        "paper_scan_tickers": [
            row["ticker"]
            for row in returned_rows
            if row.get("fresh_snapshot") and row.get("snapshot_join_status") == SNAPSHOT_JOINED
        ],
        "summary": {
            "crypto_links_seen": len(rows) + len(excluded_rows),
            "current_active_crypto_markets": len(returned_rows),
            "current_active_crypto_markets_total": len(rows),
            "current_scope_limit": limit,
            "exact_ticker_scope_count": (
                len({str(ticker).strip() for ticker in ticker_scope if str(ticker).strip()})
                if ticker_scope is not None
                else None
            ),
            "current_snapshots": sum(1 for row in returned_rows if row.get("fresh_snapshot")),
            "current_snapshot_total": sum(1 for row in rows if row.get("fresh_snapshot")),
            "paper_scan_tickers": sum(
                1
                for row in returned_rows
                if row.get("fresh_snapshot") and row.get("snapshot_join_status") == SNAPSHOT_JOINED
            ),
            "expired_crypto_window_links_excluded": sum(
                1 for row in excluded_rows if row.get("excluded_reason") == EXPIRED_WINDOW_EXCLUDED
            ),
            "market_closed_or_settled_excluded": sum(
                1 for row in excluded_rows if row.get("excluded_reason") == MARKET_CLOSED_OR_SETTLED
            ),
            "historical_rows_excluded": sum(
                1 for row in excluded_rows if row.get("excluded_reason") == "HISTORICAL_ROW_EXCLUDED"
            ),
        },
    }


def _forecast_ranking_row(
    active_row: dict[str, Any],
    *,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    latest_any_forecast: Forecast | None,
    latest_any_ranking: MarketRanking | None,
    freshness_minutes: int,
    now: datetime,
) -> dict[str, Any]:
    blocker = _forecast_ranking_blocker(
        active_row,
        forecast=forecast,
        ranking=ranking,
        latest_any_forecast=latest_any_forecast,
        latest_any_ranking=latest_any_ranking,
        freshness_minutes=freshness_minutes,
        now=now,
    )
    return {
        **active_row,
        "latest_forecast_id": forecast.id if forecast is not None else None,
        "latest_forecast_at": forecast.forecasted_at.isoformat() if forecast else None,
        "latest_forecast_model": forecast.model_name if forecast else None,
        "latest_any_forecast_model": latest_any_forecast.model_name
        if latest_any_forecast
        else None,
        "latest_ranking_id": ranking.id if ranking is not None else None,
        "latest_ranking_at": ranking.ranked_at.isoformat() if ranking else None,
        "latest_ranking_model": ranking.forecast_model if ranking else None,
        "latest_any_ranking_model": latest_any_ranking.forecast_model
        if latest_any_ranking
        else None,
        "forecast_age_minutes": _age_minutes(forecast.forecasted_at) if forecast else None,
        "ranking_age_minutes": _age_minutes(ranking.ranked_at) if ranking else None,
        "fresh_forecast": _fresh_enough(forecast.forecasted_at, minutes=freshness_minutes)
        if forecast
        else False,
        "fresh_ranking": _fresh_enough(ranking.ranked_at, minutes=freshness_minutes)
        if ranking
        else False,
        "estimated_edge": ranking.estimated_edge if ranking else None,
        "opportunity_score": ranking.opportunity_score if ranking else None,
        "spread": ranking.spread if ranking else None,
        "liquidity": ranking.liquidity if ranking else None,
        "first_hard_blocker": blocker,
        "reason_code": blocker,
    }


def _forecast_ranking_blocker(
    active_row: dict[str, Any],
    *,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    latest_any_forecast: Forecast | None,
    latest_any_ranking: MarketRanking | None,
    freshness_minutes: int,
    now: datetime,
) -> str:
    if str(active_row.get("ticker") or "").strip() != str(active_row.get("ticker") or "").strip().upper():
        return "TICKER_MISMATCH"
    if forecast is None and latest_any_forecast is not None:
        return "MODEL_NAME_MISMATCH"
    if ranking is None and latest_any_ranking is not None:
        return "MODEL_NAME_MISMATCH"
    if forecast is None and ranking is not None:
        return "RANKING_NOT_FORECASTED"
    if forecast is None:
        return "FORECAST_STALE"
    if _minutes_between(forecast.forecasted_at, now) > freshness_minutes:
        return "FORECAST_STALE"
    if ranking is None:
        return "CURRENT_FORECAST_MISSING_RANKING"
    if _minutes_between(ranking.ranked_at, now) > freshness_minutes:
        return "RANKING_STALE"
    if _aware(ranking.ranked_at) + timedelta(seconds=1) < _aware(forecast.forecasted_at):
        return "TIMESTAMP_WINDOW_MISMATCH"
    return "CURRENT_FORECAST_RANKED"


def _first_diagnostic_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_CURRENT_ACTIVE_CRYPTO_MARKETS"
    for row in rows:
        reason = str(row.get("first_hard_blocker") or "")
        if reason and reason != "CURRENT_FORECAST_RANKED":
            return reason
    return "NO_CURRENT_RANKING_BLOCKER"


def _funnel_row(
    session: Session,
    row: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, Any]:
    ticker = str(row["ticker"])
    market = session.get(Market, ticker)
    ranking = session.get(MarketRanking, row.get("latest_ranking_id")) if row.get("latest_ranking_id") else None
    snapshot = _latest_snapshot_for_ticker(session, ticker)
    identity = verify_market_identity(session, ticker=ticker, ranking=ranking, market=market, settings=settings)
    sizing = _latest_sizing(session, ticker)
    risk = _latest_risk(session, ticker)
    edge = to_decimal(row.get("estimated_edge")) or Decimal("0")
    spread = to_decimal(row.get("spread")) or Decimal("0")
    executable_ev = edge - spread
    liquidity = to_decimal(row.get("liquidity")) or Decimal("0")
    ranking_score = to_decimal(row.get("opportunity_score")) or Decimal("0")
    ranking_spread = to_decimal(row.get("spread"))
    settlement_terms = bool(market and (market.rules_primary or market.rules_secondary) and market.close_time)
    executable_book = _has_executable_book(snapshot)
    snapshot_reason = str(row.get("snapshot_join_status") or _snapshot_join_status(row))
    orderbook_reason = _orderbook_join_status(snapshot)
    stage_status = {
        "active_pure_crypto_markets": True,
        "fresh_snapshots": bool(row.get("fresh_snapshot")),
        "current_crypto_v2_forecasts": bool(row.get("latest_forecast_at"))
        and row.get("first_hard_blocker") not in {"FORECAST_STALE"},
        "current_rankings": bool(row.get("latest_ranking_at"))
        and row.get("first_hard_blocker") == "CURRENT_FORECAST_RANKED",
        "positive_raw_ev": edge > 0,
        "positive_executable_ev": executable_ev > 0,
        "verified_kalshi_link": identity.url_verification_status == VERIFIED,
        "executable_book": executable_book,
        "liquidity_pass": liquidity >= settings.opportunity_min_liquidity,
        "spread_pass": ranking_spread is None or ranking_spread <= settings.opportunity_max_spread,
        "settlement_terms_pass": settlement_terms,
        "phase3s_proceed": ranking_score >= settings.opportunity_min_score,
        "phase3m_nonzero_size": int(getattr(sizing, "proposed_contracts", 0) or 0) > 0,
        "phase3n_approval": str(getattr(risk, "action", "") or "").upper()
        in {"ALLOW", "APPROVE", "PROCEED"},
    }
    paper_ready = all(stage_status.values())
    stage_status["paper_ready_candidates"] = paper_ready
    stage_reason_codes = {
        "fresh_snapshots": snapshot_reason,
        "current_crypto_v2_forecasts": FORECAST_SNAPSHOT_JOIN_MISSING
        if row.get("fresh_snapshot") and not row.get("latest_forecast_at")
        else "FORECAST_STALE",
        "current_rankings": "CURRENT_FORECAST_MISSING_RANKING"
        if row.get("latest_forecast_at") and not row.get("latest_ranking_at")
        else "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST",
        "positive_raw_ev": "EV_NOT_POSITIVE",
        "positive_executable_ev": "EXECUTABLE_EV_NOT_POSITIVE",
        "verified_kalshi_link": "URL_UNVERIFIED",
        "executable_book": orderbook_reason,
        "liquidity_pass": "LIQUIDITY_TOO_LOW",
        "spread_pass": "SPREAD_TOO_WIDE",
        "settlement_terms_pass": "SETTLEMENT_TERMS_UNKNOWN",
        "phase3s_proceed": "RANKING_FILTERED_BY_SCORE",
        "phase3m_nonzero_size": "PHASE_3M_ZERO_SIZE",
        "phase3n_approval": "PHASE_3N_RISK_BLOCK",
        "paper_ready_candidates": "NO_PAPER_READY_CANDIDATE",
    }
    return {
        **row,
        "kalshi_url_status": identity.url_verification_status,
        "executable_ev": str(executable_ev),
        "stage_status": stage_status,
        "stage_reason_codes": stage_reason_codes,
        "first_hard_blocker": _first_false_stage(stage_status, stage_reason_codes) or "PAPER_READY",
    }


def _forecast_ranking_join_result(summary: dict[str, Any]) -> str:
    if int(summary.get("current_forecasts") or 0) <= 0:
        return "NO_CURRENT_FORECASTS"
    if int(summary.get("current_rankings") or 0) > 0:
        return "JOINED"
    return "CURRENT_FORECAST_MISSING_RANKING"


def _funnel_stages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = [
        ("active_pure_crypto_markets", "active pure crypto markets"),
        ("fresh_snapshots", "fresh snapshots"),
        ("current_crypto_v2_forecasts", "current crypto_v2 forecasts"),
        ("current_rankings", "current rankings"),
        ("positive_raw_ev", "positive raw EV"),
        ("positive_executable_ev", "positive executable EV"),
        ("verified_kalshi_link", "verified Kalshi link"),
        ("executable_book", "executable book"),
        ("liquidity_pass", "liquidity pass"),
        ("spread_pass", "spread pass"),
        ("settlement_terms_pass", "settlement terms pass"),
        ("phase3s_proceed", "Phase 3S proceed"),
        ("phase3m_nonzero_size", "Phase 3M nonzero size"),
        ("phase3n_approval", "Phase 3N approval"),
        ("paper_ready_candidates", "paper-ready candidates"),
    ]
    stages: list[dict[str, Any]] = []
    remaining = rows
    for key, label in labels:
        passed = [row for row in remaining if row["stage_status"].get(key)]
        failed = [row for row in remaining if not row["stage_status"].get(key)]
        reason_counts = Counter(
            str(row.get("stage_reason_codes", {}).get(key) or _stage_reason_code(key))
            for row in failed
        )
        stages.append(
            {
                "stage": key,
                "label": label,
                "input_rows": len(remaining),
                "pass_rows": len(passed),
                "fail_rows": len(failed),
                "reason_code": reason_counts.most_common(1)[0][0]
                if reason_counts
                else _stage_reason_code(key),
                "reason_counts": dict(sorted(reason_counts.items())),
                "examples": [
                    {
                        "ticker": row["ticker"],
                        "reason_code": str(
                            row.get("stage_reason_codes", {}).get(key)
                            or _stage_reason_code(key)
                        ),
                    }
                    for row in failed[:5]
                ],
            }
        )
        remaining = passed
    return stages


def _first_funnel_blocker(stages: list[dict[str, Any]]) -> str | None:
    for stage in stages:
        if int(stage["input_rows"]) > 0 and int(stage["pass_rows"]) == 0:
            return str(stage["reason_code"])
    for stage in stages:
        if int(stage["input_rows"]) > 0 and int(stage["fail_rows"]) > 0:
            return str(stage["reason_code"])
    return None


def _stage_reason_code(stage: str) -> str:
    return {
        "fresh_snapshots": SNAPSHOT_STALE,
        "current_crypto_v2_forecasts": "FORECAST_STALE",
        "current_rankings": "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST",
        "positive_raw_ev": "EV_NOT_POSITIVE",
        "positive_executable_ev": "EXECUTABLE_EV_NOT_POSITIVE",
        "verified_kalshi_link": "URL_UNVERIFIED",
        "executable_book": ORDERBOOK_MISSING,
        "liquidity_pass": "LIQUIDITY_TOO_LOW",
        "spread_pass": "SPREAD_TOO_WIDE",
        "settlement_terms_pass": "SETTLEMENT_TERMS_UNKNOWN",
        "phase3s_proceed": "RANKING_FILTERED_BY_SCORE",
        "phase3m_nonzero_size": "PHASE_3M_ZERO_SIZE",
        "phase3n_approval": "PHASE_3N_RISK_BLOCK",
        "paper_ready_candidates": "NO_PAPER_READY_CANDIDATE",
    }.get(stage, "UNKNOWN_REQUIRES_INVESTIGATION")


def _first_false_stage(
    stage_status: dict[str, bool],
    stage_reason_codes: dict[str, str] | None = None,
) -> str | None:
    for key, value in stage_status.items():
        if not value:
            if stage_reason_codes and stage_reason_codes.get(key):
                return stage_reason_codes[key]
            return _stage_reason_code(key)
    return None


def _router_row(
    active_row: dict[str, Any],
    *,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    opportunity: MarketOpportunity | None,
    target: LearningTradeTarget | None,
    paper_orders: list[PaperOrder],
    skip: ForecastSkipLog | None,
) -> dict[str, Any]:
    status = _router_status(
        active_row=active_row,
        forecast=forecast,
        ranking=ranking,
        opportunity=opportunity,
        target=target,
        paper_orders=paper_orders,
        skip=skip,
    )
    return {
        **active_row,
        "latest_forecast_at": forecast.forecasted_at.isoformat() if forecast else None,
        "latest_skip_reason": skip.reason if skip is not None else None,
        "latest_skip_at": skip.skipped_at.isoformat() if skip is not None else None,
        "latest_ranking_at": ranking.ranked_at.isoformat() if ranking else None,
        "opportunity_score": ranking.opportunity_score if ranking else None,
        "latest_opportunity_at": opportunity.detected_at.isoformat() if opportunity else None,
        "learning_target_at": target.generated_at.isoformat() if target else None,
        "learning_priority_score": target.learning_priority_score if target else None,
        "paper_orders": len(paper_orders),
        "router_status": status,
        "next_action": _router_row_next_action(status, skip),
    }


def _router_status(
    *,
    active_row: dict[str, Any],
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    opportunity: MarketOpportunity | None,
    target: LearningTradeTarget | None,
    paper_orders: list[PaperOrder],
    skip: ForecastSkipLog | None,
) -> str:
    if not active_row["has_snapshot"]:
        return "missing_active_snapshot"
    if forecast is None:
        return str(skip.reason) if skip is not None else "missing_crypto_v2_forecast"
    if ranking is None:
        return "forecast_not_ranked"
    if opportunity is None:
        return "no_positive_ev_opportunity"
    if target is None:
        return "not_selected_for_learning"
    if not paper_orders:
        return "no_paper_trade_created"
    return "paper_trade_created"


def _router_row_next_action(status: str, skip: ForecastSkipLog | None) -> str:
    if status == "missing_active_snapshot":
        return "Run collect-once for open markets, then forecast crypto_v2."
    if status in {"missing_crypto_v2_forecast", "insufficient_feature_history"}:
        return "Run crypto-history-warmup, collect fresh snapshots, then forecast crypto_v2."
    if skip is not None and "feature" in str(skip.reason).lower():
        return "Refresh/warm crypto features, then rerun crypto_v2."
    if status == "forecast_not_ranked":
        return "Run find-opportunities --model-name crypto_v2."
    if status == "no_positive_ev_opportunity":
        return "No paper trade until opportunity thresholds are met."
    if status == "not_selected_for_learning":
        return "Run learning-targets with Learning Mode enabled."
    if status == "no_paper_trade_created":
        return "Run paper-run --model-name crypto_v2 if cap/risk gates allow it."
    return "Monitor paper-only settlement outcome."


def _symbol_feature_row(
    session: Session,
    *,
    symbol: str,
    required_history_minutes: int,
) -> dict[str, Any]:
    feature = session.scalar(
        select(CryptoFeature)
        .where(CryptoFeature.symbol == normalize_symbol(symbol))
        .order_by(desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
        .limit(1)
    )
    if feature is None:
        return {
            "symbol": symbol,
            "status": "MISSING",
            "history_minutes": 0,
            "latest_feature_id": None,
            "latest_generated_at": None,
            "quality_flags": ["missing_feature"],
        }
    raw = decode_json(feature.raw_json)
    history = _int(raw.get("history_minutes"))
    flags = raw.get("quality_flags") if isinstance(raw.get("quality_flags"), list) else []
    status = (
        "READY"
        if history >= required_history_minutes and feature.momentum_score
        else "BLOCKED"
    )
    return {
        "symbol": symbol,
        "status": status,
        "history_minutes": history,
        "latest_feature_id": feature.id,
        "latest_generated_at": feature.generated_at.isoformat(),
        "quality_flags": [str(flag) for flag in flags] or ["ok"],
        "source": feature.source,
        "momentum_score": feature.momentum_score,
    }


def _latest_by_ticker(
    session: Session,
    table: Any,
    *,
    tickers: list[str],
    model_field: str,
    model_name: str,
    time_field: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    statement = (
        select(table)
        .where(table.ticker.in_(tickers), getattr(table, model_field) == model_name)
        .order_by(table.ticker, desc(getattr(table, time_field)), desc(table.id))
    )
    latest: dict[str, Any] = {}
    for row in session.scalars(statement):
        latest.setdefault(row.ticker, row)
    return latest


def _latest_rows_by_ticker(
    session: Session,
    table: Any,
    *,
    tickers: list[str],
    time_field: str,
) -> dict[str, Any]:
    if not tickers:
        return {}
    statement = (
        select(table)
        .where(table.ticker.in_(tickers))
        .order_by(table.ticker, desc(getattr(table, time_field)), desc(table.id))
    )
    latest: dict[str, Any] = {}
    for row in session.scalars(statement):
        latest.setdefault(row.ticker, row)
    return latest


def _count_model_tickers(
    session: Session,
    table: Any,
    *,
    tickers: list[str],
    model_field: str,
    model_name: str,
) -> int:
    count = 0
    for chunk in _chunks(sorted({ticker for ticker in tickers if ticker}), 900):
        count += int(
            session.scalar(
                select(func.count(func.distinct(table.ticker))).where(
                    table.ticker.in_(chunk),
                    getattr(table, model_field) == model_name,
                )
            )
            or 0
        )
    return count


def _markets_by_ticker(session: Session, tickers: list[str]) -> dict[str, Market]:
    markets: dict[str, Market] = {}
    for chunk in _chunks(sorted({ticker for ticker in tickers if ticker}), 900):
        for market in session.scalars(select(Market).where(Market.ticker.in_(chunk))):
            markets[market.ticker] = market
    return markets


def _latest_snapshots_for_tickers(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    latest: dict[str, MarketSnapshot] = {}
    for chunk in _chunks(sorted({ticker for ticker in tickers if ticker}), 900):
        statement = (
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker.in_(chunk))
            .order_by(
                MarketSnapshot.ticker,
                desc(MarketSnapshot.captured_at),
                desc(MarketSnapshot.id),
            )
        )
        for snapshot in session.scalars(statement):
            latest.setdefault(snapshot.ticker, snapshot)
    return latest


def _latest_snapshot_for_ticker(session: Session, ticker: str) -> Any | None:
    return session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(1)
    )


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _current_scope_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    fresh_rank = 0 if row.get("fresh_snapshot") else 1
    snapshot_at = str(row.get("latest_snapshot_at") or "")
    close_time = str(row.get("market_close_time") or "")
    return (fresh_rank, _reverse_sort_text(snapshot_at), close_time)


def _reverse_sort_text(value: str) -> str:
    return "".join(chr(255 - ord(char)) for char in value)


def _snapshot_join_status(row: dict[str, Any]) -> str:
    if not row.get("has_snapshot"):
        return NO_CURRENT_SNAPSHOT
    if row.get("window_status") not in {None, "CURRENT_WINDOW"}:
        return TICKER_WINDOW_MISMATCH
    if not row.get("fresh_snapshot"):
        return SNAPSHOT_STALE
    return SNAPSHOT_JOINED


def _orderbook_join_status(snapshot: MarketSnapshot | None) -> str:
    if snapshot is None:
        return NO_CURRENT_SNAPSHOT
    if not snapshot.raw_orderbook_json:
        return ORDERBOOK_MISSING
    if not _has_visible_bid_ask(snapshot):
        return ORDERBOOK_MISSING
    return ORDERBOOK_JOINED


def _has_visible_bid_ask(snapshot: MarketSnapshot | None) -> bool:
    if snapshot is None:
        return False
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    no_bid = to_decimal(snapshot.best_no_bid)
    no_ask = to_decimal(snapshot.best_no_ask)
    return (yes_bid is not None and yes_ask is not None) or (
        no_bid is not None and no_ask is not None
    )


def _fresh_enough(value: Any, *, minutes: int) -> bool:
    age = _age_minutes(value)
    return age is not None and age <= minutes


def _fresh_enough_at(value: Any, *, minutes: int, now: datetime) -> bool:
    age = _age_minutes_at(value, now=now)
    return age is not None and age <= minutes


def _age_minutes(value: Any) -> float | None:
    if value is None:
        return None
    dt = _aware(value)
    return max(0.0, (utc_now() - dt).total_seconds() / 60)


def _age_minutes_at(value: Any, *, now: datetime) -> float | None:
    if value is None:
        return None
    return max(0.0, (_aware(now) - _aware(value)).total_seconds() / 60)


def _minutes_between(value: Any, now: datetime) -> float:
    if value is None:
        return float("inf")
    return max(0.0, (_aware(now) - _aware(value)).total_seconds() / 60)


def _latest_sizing(session: Session, ticker: str) -> PositionSizingDecisionLog | None:
    return session.scalar(
        select(PositionSizingDecisionLog)
        .where(PositionSizingDecisionLog.ticker == ticker)
        .order_by(desc(PositionSizingDecisionLog.decision_timestamp), desc(PositionSizingDecisionLog.id))
        .limit(1)
    )


def _latest_risk(session: Session, ticker: str) -> AdvancedRiskDecisionLog | None:
    return session.scalar(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker == ticker)
        .order_by(desc(AdvancedRiskDecisionLog.decision_timestamp), desc(AdvancedRiskDecisionLog.id))
        .limit(1)
    )


def _has_executable_book(snapshot: MarketSnapshot | None) -> bool:
    if snapshot is None or not snapshot.raw_orderbook_json:
        return False
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    no_bid = to_decimal(snapshot.best_no_bid)
    no_ask = to_decimal(snapshot.best_no_ask)
    return (yes_bid is not None and yes_ask is not None) or (
        no_bid is not None and no_ask is not None
    )


def _orders_by_ticker(
    session: Session,
    *,
    tickers: list[str],
    model_name: str,
) -> dict[str, list[PaperOrder]]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(PaperOrder).where(
            PaperOrder.ticker.in_(tickers),
            PaperOrder.model_name == model_name,
        )
    )
    grouped: dict[str, list[PaperOrder]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)
    return grouped


def _latest_skips_by_ticker(
    session: Session,
    *,
    tickers: list[str],
) -> dict[str, ForecastSkipLog]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(ForecastSkipLog)
        .where(ForecastSkipLog.ticker.in_(tickers), ForecastSkipLog.model_name == "crypto_v2")
        .order_by(
            ForecastSkipLog.ticker,
            desc(ForecastSkipLog.skipped_at),
            desc(ForecastSkipLog.id),
        )
    )
    latest: dict[str, ForecastSkipLog] = {}
    for row in rows:
        latest.setdefault(row.ticker, row)
    return latest


def _component_symbols(raw: dict[str, Any], fallback: str) -> list[str]:
    terms = raw.get("structured_terms")
    symbols: list[str] = []
    if isinstance(terms, dict) and isinstance(terms.get("component_symbols"), list):
        symbols = [normalize_symbol(str(symbol)) for symbol in terms["component_symbols"]]
    if not symbols:
        symbols = [normalize_symbol(part) for part in fallback.split("+") if part.strip()]
    return sorted(set(symbols))


def _row_symbols(row: dict[str, Any]) -> set[str]:
    return {normalize_symbol(str(symbol)) for symbol in row.get("component_symbols", [])}


def _symbols_from_router_rows(rows: list[dict[str, Any]]) -> set[str]:
    symbols: set[str] = set()
    for row in rows:
        symbols.update(_row_symbols(row))
    return symbols


def _warmup_next_action(
    rows: list[dict[str, Any]],
    feature_rows: list[dict[str, Any]],
) -> str:
    if any(row["status"] == "MISSING_LATEST_PRICE" for row in rows):
        return "Ingest missing crypto prices, then rerun history warmup."
    if any(row["status"] != "READY" for row in feature_rows):
        return "Warmup ran but features are still incomplete; inspect feature rows."
    return "History is warmed; collect fresh active snapshots and rerun crypto_v2."


def _router_next_action(
    rows: list[dict[str, Any]],
    feature_rows: list[dict[str, Any]],
) -> str:
    if any(row["status"] != "READY" for row in feature_rows):
        return "Run crypto-history-warmup before forecasting active crypto markets."
    counts = Counter(row["router_status"] for row in rows)
    main = counts.most_common(1)[0][0] if counts else None
    if main in {"missing_crypto_v2_forecast", "insufficient_feature_history"}:
        return "Warm feature history, collect fresh snapshots, and rerun crypto_v2."
    if main == "forecast_not_ranked":
        return "Run find-opportunities for crypto_v2."
    if main == "no_positive_ev_opportunity":
        return "No paper trades until active crypto forecasts clear opportunity thresholds."
    if main == "not_selected_for_learning":
        return "Run learning-targets with Learning Mode enabled."
    if main == "no_paper_trade_created":
        return "Run paper-run --model-name crypto_v2 if paper cap and risk gates allow it."
    return "Crypto active funnel is connected; monitor paper-only outcomes."


def _render_warmup_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AT Crypto History Warmup",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Symbols",
            "",
            "| Symbol | Status | History before | History after | Rows inserted | Next action |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in payload["rows"]:
        lines.append(
            f"| {row['symbol']} | {row['status']} | {row['history_before_minutes']} | "
            f"{row['history_after_minutes']} | {row['price_rows_inserted']} | "
            f"{row['next_action']} |"
        )
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        ["```", "", "## Recommended Next Action", "", payload["recommended_next_action"], ""]
    )
    return "\n".join(lines)


def _render_router_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AT Active Forecast-to-Opportunity Router",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "",
        "## Funnel Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Router Status Counts", ""])
    for key, value in payload["router_status_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Feature History",
            "",
            "| Symbol | Status | History minutes | Feature | Flags |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for row in payload["feature_rows"]:
        flags = ",".join(row["quality_flags"])
        lines.append(
            f"| {row['symbol']} | {row['status']} | {row['history_minutes']} | "
            f"{row['latest_feature_id'] or 'none'} | {flags} |"
        )
    lines.extend(
        [
            "",
            "## Blocked Active Examples",
            "",
            "| Ticker | Symbols | Status | Skip | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["blocked_examples"][:25]:
        lines.append(
            f"| {row['ticker']} | {','.join(row['component_symbols'])} | "
            f"{row['router_status']} | {row['latest_skip_reason'] or 'none'} | "
            f"{row['next_action']} |"
        )
    if not payload["blocked_examples"]:
        lines.append("| n/a | n/a | connected | none | Monitor paper outcomes. |")
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        ["```", "", "## Recommended Next Action", "", payload["recommended_next_action"], ""]
    )
    return "\n".join(lines)


def _render_forecast_ranking_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AT Forecast-Ranking Diagnostic",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "- Paper trade creation: blocked by this diagnostic.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Reason Counts", ""])
    for key, value in payload["reason_counts"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Current Rows",
            "",
            "| Ticker | Forecast | Ranking | Reason | Snapshot |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in payload["current_rows"][:50]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row.get('latest_forecast_at') or 'missing'} | "
            f"{row.get('latest_ranking_at') or 'missing'} | "
            f"{row.get('first_hard_blocker') or 'n/a'} | "
            f"{row.get('latest_snapshot_at') or 'missing'} |"
        )
    if not payload["current_rows"]:
        lines.append("| n/a | n/a | n/a | NO_CURRENT_ACTIVE_CRYPTO_MARKETS | n/a |")
    lines.extend(
        [
            "",
            "## Excluded Historical Rows",
            "",
            "| Ticker | Reason | Close time | Expiration |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["excluded_rows"][:50]:
        lines.append(
            "| "
            f"{row.get('ticker') or 'n/a'} | "
            f"{row.get('excluded_reason') or 'n/a'} | "
            f"{row.get('market_close_time') or 'n/a'} | "
            f"{row.get('expected_expiration_time') or 'n/a'} |"
        )
    if not payload["excluded_rows"]:
        lines.append("| n/a | none | n/a | n/a |")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_funnel_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AT Opportunity Funnel",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "- Paper trade creation: blocked by this diagnostic.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Stages",
            "",
            "| Stage | Input | Pass | Fail | Reason | Examples |",
            "| --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for stage in payload["stages"]:
        examples = _format_stage_examples(stage.get("examples") or [])
        lines.append(
            "| "
            f"{stage['label']} | "
            f"{stage['input_rows']} | "
            f"{stage['pass_rows']} | "
            f"{stage['fail_rows']} | "
            f"{stage['reason_code']} | "
            f"{examples} |"
        )
    lines.extend(
        [
            "",
            "## Blocked Current Examples",
            "",
            "| Ticker | First blocker | Forecast | Ranking | Executable EV |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    blocked = [row for row in payload["rows"] if row.get("first_hard_blocker") != "PAPER_READY"]
    for row in blocked[:50]:
        lines.append(
            "| "
            f"{row['ticker']} | "
            f"{row.get('first_hard_blocker') or 'n/a'} | "
            f"{row.get('latest_forecast_at') or 'missing'} | "
            f"{row.get('latest_ranking_at') or 'missing'} | "
            f"{row.get('executable_ev') or 'n/a'} |"
        )
    if not blocked:
        lines.append("| n/a | PAPER_READY | n/a | n/a | n/a |")
    lines.extend(["", "## Next Action", "", payload["next_action"], ""])
    return "\n".join(lines)


def _render_handoff_summary(
    *,
    diagnostic: dict[str, Any],
    funnel: dict[str, Any],
    performance: dict[str, Any],
) -> str:
    diag = diagnostic["summary"]
    fun = funnel["summary"]
    current_forecasts = int(diag.get("current_forecasts") or 0)
    current_rankings = int(diag.get("current_rankings") or 0)
    missing_rankings = int(diag.get("forecast_tickers_missing_rankings") or 0)
    opportunity_count = int(fun.get("opportunity_count") or 0)
    excluded = int(diag.get("expired_historical_rows_excluded") or 0)
    first_hard_blocker = str(fun.get("first_hard_blocker") or diag.get("first_hard_blocker"))
    next_action = _phase3at_next_action({"first_hard_blocker": first_hard_blocker})
    lines = [
        "# Phase 3AT Current-Window Handoff",
        "",
        f"- Generated at: {diagnostic['generated_at']}",
        f"- Safety: {diagnostic['paper_only_safety']}",
        "- Live/demo execution: blocked.",
        "- This report does not submit, cancel, replace, or amend orders.",
        "",
        "## Answers",
        "",
        (
            "- Why forecasts produced fewer rankings: "
            f"{current_forecasts} current forecasts and {current_rankings} current rankings "
            f"were found; {missing_rankings} current forecasts are missing rankings."
        ),
        (
            "- Why rankings produced opportunities: "
            f"{current_rankings} current rankings produced {opportunity_count} "
            "paper-ready candidate(s) through the current funnel."
        ),
        (
            "- Historical pollution: "
            f"{excluded} expired, closed, or historical crypto row(s) were excluded "
            "from current paper mode."
        ),
        f"- Current active crypto markets: {diag.get('current_active_crypto_markets')}",
        f"- Current snapshots: {diag.get('current_snapshots')}",
        f"- First hard blocker: {first_hard_blocker}",
        f"- No-trade correct right now: {fun.get('no_trade_correct')}",
        f"- Performance status: {performance.get('performance_status')}",
        (
            "- Current scan scope: "
            f"{performance.get('current_scan_scope_count')} row(s); "
            f"historical rows excluded: {performance.get('historical_rows_excluded')}."
        ),
        "",
        "## Next Operator Command",
        "",
        "```bash",
        next_action,
        "```",
        "",
    ]
    return "\n".join(lines)


def _render_handoff_next_actions(
    diagnostic: dict[str, Any],
    funnel: dict[str, Any],
) -> str:
    first_hard_blocker = (
        funnel["summary"].get("first_hard_blocker")
        or diagnostic["summary"].get("first_hard_blocker")
    )
    command = _phase3at_next_action({"first_hard_blocker": first_hard_blocker})
    lines = [
        "# Phase 3AT Next Actions",
        "",
        "Registered command only:",
        "",
        "```bash",
        command,
        "```",
        "",
        f"First hard blocker: {first_hard_blocker}",
        "",
        "Safety: paper/read-only diagnostics; no live/demo exchange writes.",
        "",
    ]
    return "\n".join(lines)


def _phase3at_next_action(summary: dict[str, Any]) -> str:
    blocker = str(summary.get("first_hard_blocker") or "")
    if blocker == R5_PRIMARY_EV_NOT_POSITIVE:
        return "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"
    if blocker in {
        "NO_CURRENT_ACTIVE_CRYPTO_MARKETS",
        EXPIRED_WINDOW_EXCLUDED,
        MARKET_CLOSED_OR_SETTLED,
        "HISTORICAL_ROW_EXCLUDED",
        NO_CURRENT_SNAPSHOT,
        SNAPSHOT_STALE,
        SNAPSHOT_EXISTS_BUT_NOT_JOINED,
        TICKER_WINDOW_MISMATCH,
    }:
        return (
            "kalshi-bot phase3bc-r3-active-crypto-refresh "
            "--output-dir reports/phase3bc_r3 --forecast-current-windows-only"
        )
    if blocker in {"FORECAST_STALE", "BOOK_MISSING", ORDERBOOK_MISSING}:
        return (
            "kalshi-bot phase3bc-r3-active-crypto-refresh "
            "--output-dir reports/phase3bc_r3 --forecast-current-windows-only"
        )
    if blocker in {
        "FORECAST_NOT_RANKED",
        "CURRENT_FORECAST_MISSING_RANKING",
        "RANKING_NOT_GENERATED_FOR_CURRENT_FORECAST",
        "RANKING_STALE",
        "TIMESTAMP_WINDOW_MISMATCH",
        "MODEL_NAME_MISMATCH",
        "RANKING_NOT_FORECASTED",
    }:
        return (
            "kalshi-bot phase3bc-r3-active-crypto-refresh "
            "--output-dir reports/phase3bc_r3 --forecast-current-windows-only "
            "--diagnose-snapshots --generate-opportunity-report"
        )
    return (
        "kalshi-bot phase3at-handoff-report "
        "--output-dir reports/phase3at --reports-dir reports"
    )


def _report_metadata(
    session: Session,
    *,
    settings: Settings,
    command_args: list[str] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    db_url = _database_url(session)
    return {
        "generated_at": (generated_at or utc_now()).isoformat(),
        "git_commit": _git_commit(),
        "db_fingerprint": _db_fingerprint(session, db_url),
        "db_url_redacted": db_url,
        "command_args": command_args or [],
        "data_watermark": _data_watermark(session),
        "safety_flags": {
            "paper_only": True,
            "live_demo_execution_blocked": True,
            "order_submission_cancel_replace_blocked": True,
            "thresholds_lowered": False,
            "paper_trades_created_by_report": False,
        },
        "settings": {
            "opportunity_min_edge": str(settings.opportunity_min_edge),
            "opportunity_min_score": str(settings.opportunity_min_score),
            "opportunity_max_spread": str(settings.opportunity_max_spread),
            "opportunity_min_liquidity": str(settings.opportunity_min_liquidity),
            "opportunity_min_time_to_close_minutes": str(
                settings.opportunity_min_time_to_close_minutes
            ),
        },
    }


def _database_url(session: Session) -> str:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is None:
        return "unknown"
    try:
        return str(url.render_as_string(hide_password=True))
    except AttributeError:
        return str(url)


def _db_fingerprint(session: Session, db_url: str) -> str:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    database = getattr(url, "database", None)
    parts = [db_url]
    if database:
        path = Path(str(database))
        if path.exists():
            stat = path.stat()
            parts.extend([str(path.resolve()), str(stat.st_size), str(int(stat.st_mtime))])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _data_watermark(session: Session) -> dict[str, Any]:
    return {
        "market_snapshot_max_captured_at": _scalar_iso(
            session.scalar(select(func.max(MarketSnapshot.captured_at)))
        ),
        "forecast_max_forecasted_at": _scalar_iso(
            session.scalar(select(func.max(Forecast.forecasted_at)))
        ),
        "ranking_max_ranked_at": _scalar_iso(
            session.scalar(select(func.max(MarketRanking.ranked_at)))
        ),
        "crypto_link_max_detected_at": _scalar_iso(
            session.scalar(select(func.max(CryptoMarketLink.detected_at)))
        ),
    }


def _scalar_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _git_commit() -> str:
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if not (candidate / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=candidate,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() or "unknown"
        except Exception:
            return "unknown"
    return "unknown"


def _format_stage_examples(examples: list[Any]) -> str:
    if not examples:
        return "none"
    rendered: list[str] = []
    for example in examples:
        if isinstance(example, dict):
            rendered.append(
                f"{example.get('ticker', 'n/a')}:{example.get('reason_code', 'n/a')}"
            )
        else:
            rendered.append(str(example))
    return ", ".join(rendered)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["status"]
        rows = [{"status": "NO_ROWS"}]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines: list[str] = []
    for artifact in files:
        if not artifact.exists():
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
