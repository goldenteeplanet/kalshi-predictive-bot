from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import market_status_bucket
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import supported_crypto_asset, symbol_from_event_ticker
from kalshi_predictor.crypto.semantics import (
    AMBIGUOUS,
    EXACT_LINK,
    UNSUPPORTED,
    parse_crypto_market_terms,
)
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.backend import database_url_from_settings, sqlite_path_from_url
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    CryptoFeature,
    CryptoMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    PositionSizingDecisionLog,
)
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book
from kalshi_predictor.opportunities.payout_scoring import payout_metrics_from_ranking
from kalshi_predictor.paper.models import BUY_NO, BUY_YES
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3bc_r3 import (
    DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    DEFAULT_MARKET_PAGE_LIMIT,
    DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    write_phase3bc_r3_active_crypto_refresh_report,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3AK_VERSION = "phase3ak_crypto_window_orchestrator_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase_3ak")
DEFAULT_REPORT_PATH = Path("reports/phase_3ak_report.md")
DEFAULT_CRYPTO_WATCH_STATUS_PATH = Path("reports/phase3bc_r5/phase3bc_r5_status.json")
DEFAULT_CRYPTO_WATCH_REPORT_PATH = Path(
    "reports/phase3bc_r5/phase3bc_r5_crypto_freshness_watch.json"
)
DEFAULT_PHASE3AK_REPORT_CACHE_SECONDS = 120
MODEL_NAME = "crypto_v2"
DEFAULT_PHASE3AK_SYMBOLS = "BTC,ETH"
MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("30")
MIN_EXECUTABLE_CONFIDENCE_SCORE = Decimal("40")
TARGET_PRICE_RE = re.compile(r"-(?P<comparator>B|T)(?P<target>\d+(?:\.\d+)?)(?:$|-)")
WRITER_PROCESS_MARKERS = (
    "phase3bc-r5-crypto-freshness-watch",
    "phase3bc-r5-unattended-start",
    "market-data-refresh",
    "crypto-window-sync",
)
NOT_MULTILEG = "NOT_MULTILEG"
MULTILEG_REQUIRES_COMPONENT_PROVENANCE = "MULTILEG_REQUIRES_COMPONENT_PROVENANCE"


@dataclass(frozen=True)
class Phase3AKArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path | None = None


@dataclass(frozen=True)
class Phase3AKReportArtifactSet:
    json_path: Path
    markdown_path: Path


def build_crypto_window_sync(
    session: Session,
    *,
    scope: str = "active",
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    limit: int = 5000,
    now: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = now or utc_now()
    requested_symbols = _parse_symbols(symbols)
    markets = _crypto_markets(session, symbols=requested_symbols, scope=scope, limit=limit)
    tickers = [market.ticker for market in markets]
    links = _latest_crypto_links(session, tickers)
    legs = _legs_by_ticker(session, tickers)
    snapshots = _latest_snapshots(session, tickers)
    forecasts = _latest_forecasts(session, tickers)
    rankings = _latest_rankings(session, tickers)
    sizing = _latest_sizing(session, tickers)
    risk = _latest_risk(session, tickers)
    features = _latest_features(session, requested_symbols)
    rows = [
        _window_row(
            market,
            link=links.get(market.ticker),
            legs=legs.get(market.ticker, []),
            snapshot=snapshots.get(market.ticker),
            forecast=forecasts.get(market.ticker),
            ranking=rankings.get(market.ticker),
            sizing=sizing.get(market.ticker),
            risk=risk.get(market.ticker),
            latest_features=features,
            now=generated_at,
            freshness_minutes=freshness_minutes,
            settings=resolved,
        )
        for market in markets
    ]
    rows.sort(key=lambda row: (str(row["asset"] or ""), str(row["close_time"] or ""), row["ticker"]))
    summary = _window_summary(rows)
    blocker = _primary_blocker(summary)
    payload = _with_metadata(
        {
            "phase": "3AK",
            "phase_version": PHASE3AK_VERSION,
            "mode": "PAPER_ONLY_CRYPTO_WINDOW_SYNC",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "live_or_demo_execution": False,
            "scope": scope,
            "symbols": requested_symbols,
            "freshness_minutes": freshness_minutes,
            "summary": summary,
            "primary_blocker": blocker,
            "diagnosis": _diagnosis(summary),
            "readiness_funnel": _readiness_funnel(rows),
            "state_counts": dict(sorted(Counter(row["window_state"] for row in rows).items())),
            "blocker_counts": dict(sorted(Counter(row["readiness_reason"] for row in rows).items())),
            "rows": rows,
            "next_action": _next_action_for_blocker(blocker),
            "idempotency": {
                "row_key": "ticker+window_key",
                "duplicate_window_rows": _duplicate_count([row["window_key"] for row in rows]),
                "db_writes": False,
                "artifact_only": True,
            },
        },
        session=session,
        settings=resolved,
    )
    return payload


def write_crypto_window_sync_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    scope: str = "active",
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    limit: int = 5000,
    settings: Settings | None = None,
) -> Phase3AKArtifactSet:
    payload = build_crypto_window_sync(
        session,
        scope=scope,
        symbols=symbols,
        freshness_minutes=freshness_minutes,
        limit=limit,
        settings=settings,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "crypto_window_sync.json"
    rows_path = output_dir / "crypto_windows.json"
    markdown_path = output_dir / "crypto_window_sync.md"
    _write_json(json_path, {k: v for k, v in payload.items() if k != "rows"})
    _write_json(rows_path, payload["rows"])
    markdown_path.write_text(_render_window_markdown(payload), encoding="utf-8")
    return Phase3AKArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_crypto_watch_status(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    watch_status_path: Path = DEFAULT_CRYPTO_WATCH_STATUS_PATH,
    watch_report_path: Path = DEFAULT_CRYPTO_WATCH_REPORT_PATH,
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    window_payload = build_crypto_window_sync(
        session,
        symbols=symbols,
        freshness_minutes=freshness_minutes,
        settings=resolved,
    )
    status_payload = _read_json(watch_status_path)
    report_payload = _read_json(watch_report_path)
    guard = status_payload.get("guard") if isinstance(status_payload.get("guard"), dict) else {}
    latest_summary = (
        status_payload.get("latest_summary")
        if isinstance(status_payload.get("latest_summary"), dict)
        else {}
    )
    report_summary = (
        report_payload.get("summary") if isinstance(report_payload.get("summary"), dict) else {}
    )
    status_generated_at = parse_datetime(status_payload.get("generated_at"))
    status_age_seconds = (
        max(0, int((generated_at - status_generated_at).total_seconds()))
        if status_generated_at is not None
        else None
    )
    latest_generated_at = parse_datetime(
        guard.get("latest_generated_at")
        or status_payload.get("latest_report_generated_at")
        or report_payload.get("generated_at")
    )
    latest_success_at = latest_generated_at
    latest_age_seconds = (
        max(0, int((generated_at - latest_generated_at).total_seconds()))
        if latest_generated_at is not None
        else None
    )
    runner_status = str(guard.get("status") or status_payload.get("process", {}).get("status") or "UNKNOWN")
    runner_running = bool(guard.get("running") or status_payload.get("process", {}).get("pid_running"))
    freshness_seconds = freshness_minutes * 60
    cycle_overdue_seconds = (
        max(0, latest_age_seconds - freshness_seconds)
        if latest_age_seconds is not None
        else None
    )
    heartbeat_state = _heartbeat_state(
        runner_status=runner_status,
        runner_running=runner_running,
        latest_age_seconds=latest_age_seconds,
        freshness_minutes=freshness_minutes,
        stale_report=bool(guard.get("stale_report")),
        seconds_until_timeout=_int_or_none(guard.get("seconds_until_timeout")),
    )
    watcher_summary = {**report_summary, **latest_summary}
    primary_blocker = _watch_primary_blocker(
        window_payload["summary"],
        watcher_summary,
        heartbeat_state=heartbeat_state,
    )
    window_summary = window_payload["summary"]
    watch_state = (
        heartbeat_state
        if primary_blocker == "WINDOW_SYNC_STALE"
        else _watch_state_for_blocker(primary_blocker)
    )
    next_action = _watch_next_action(primary_blocker, heartbeat_state=heartbeat_state)
    payload = _with_metadata(
        {
            "phase": "3AK",
            "phase_version": PHASE3AK_VERSION,
            "mode": "PAPER_ONLY_CRYPTO_WATCH_STATUS",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "live_or_demo_execution": False,
            "runner_state": heartbeat_state,
            "runner_status": runner_status,
            "runner_running": runner_running,
            "runner_pid": guard.get("pid") or status_payload.get("pid"),
            "runner_heartbeat": {
                "latest_generated_at": latest_generated_at.isoformat() if latest_generated_at else None,
                "latest_age_seconds": latest_age_seconds,
                "freshness_seconds": freshness_seconds,
                "last_status_check": status_generated_at.isoformat() if status_generated_at else None,
                "status_age_seconds": status_age_seconds,
                "last_attempt": status_payload.get("generated_at") or report_payload.get("generated_at"),
                "last_success": latest_success_at.isoformat() if latest_success_at else None,
                "next_scheduled_scan": (
                    (latest_success_at + timedelta(minutes=freshness_minutes)).isoformat()
                    if latest_success_at is not None
                    else None
                ),
                "cycle_overdue_seconds": cycle_overdue_seconds,
                "stale_report": bool(guard.get("stale_report")),
                "seconds_until_timeout": _int_or_none(guard.get("seconds_until_timeout")),
            },
            "active_database_writer": _writer_status(settings=resolved),
            "window_summary": window_summary,
            "watch_summary": watcher_summary,
            "readiness_funnel": window_payload["readiness_funnel"],
            "primary_blocker": primary_blocker,
            "watch_state": watch_state,
            "actionability_gap": primary_blocker,
            "current_active_window_rows": window_summary["active_windows"],
            "expired_crypto_window_rows": window_summary["expired_windows"],
            "stale_quote_rows": window_summary["stale_quote_count"],
            "paper_ready_candidates": window_summary["paper_ready_opportunities"],
            "positive_ev_rows": window_summary["positive_raw_ev"],
            "positive_executable_ev_rows": window_summary["positive_executable_ev"],
            "usable_bid_ask_books": window_summary["usable_bid_ask_books"],
            "primary_gap": primary_blocker,
            "next_action": next_action,
            "status_sources": {
                "phase3ak_window_sync": str(output_dir / "crypto_window_sync.json"),
                "phase3bc_r5_status": str(watch_status_path),
                "phase3bc_r5_report": str(watch_report_path),
            },
        },
        session=session,
        settings=resolved,
    )
    return payload


def write_crypto_watch_status_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
) -> Phase3AKArtifactSet:
    payload = build_crypto_watch_status(
        session,
        output_dir=output_dir,
        settings=settings,
        symbols=symbols,
        freshness_minutes=freshness_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "crypto_watch_status.json"
    markdown_path = output_dir / "crypto_watch_status.md"
    _write_json(json_path, payload)
    markdown_path.write_text(_render_watch_markdown(payload), encoding="utf-8")
    return Phase3AKArtifactSet(output_dir, json_path, markdown_path)


def build_market_data_refresh_status(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    bounded: bool = True,
    max_duration_seconds: int = 120,
    require_no_active_writer: bool = True,
    run_refresh: bool = True,
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    writer = _writer_status(settings=resolved)
    generated_at = utc_now()
    watermark = _market_data_watermark(session, freshness_minutes=DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES)
    blocked = require_no_active_writer and not writer["safe_to_write"]
    state = "BLOCKED_BY_ACTIVE_WRITER" if blocked else "READY_TO_REFRESH"
    refresh_summary: dict[str, Any] | None = None
    refresh_started = False
    refresh_completed = False
    refresh_error = None
    if not blocked and run_refresh:
        try:
            refresh_started = True
            artifacts = write_phase3bc_r3_active_crypto_refresh_report(
                session,
                output_dir=output_dir / "phase3bc_r3",
                settings=resolved,
                symbols=symbols,
                refresh_open_markets=True,
                external_crypto_ingest=True,
                repair_snapshots=True,
                forecast_current_windows_only=True,
                generate_opportunity_report=False,
                market_limit=DEFAULT_MARKET_PAGE_LIMIT,
                market_max_pages=1,
                crypto_market_scan_limit=DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
                crypto_link_limit=DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
                forecast_limit=2000,
                opportunity_limit=1000,
                phase3bc_limit=2000,
                near_money_only=True,
                near_money_per_symbol_limit=DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
                near_money_window_limit=DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
                snapshot_fetch_concurrency=DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
            )
            refresh_completed = True
            r3_payload = _read_json(artifacts.json_path)
            rate_limit = (
                r3_payload.get("rate_limit")
                if isinstance(r3_payload.get("rate_limit"), dict)
                else {}
            )
            summary = (
                r3_payload.get("summary")
                if isinstance(r3_payload.get("summary"), dict)
                else {}
            )
            data_complete = not bool(rate_limit.get("rate_limited")) and summary.get("data_complete") is not False
            state = "REFRESH_COMPLETED" if data_complete else "RATE_LIMITED_KALSHI_API"
            refresh_summary = {
                "artifact": str(artifacts.json_path),
                "rate_limit": rate_limit,
                "data_complete": data_complete,
                "data_completeness": "complete" if data_complete else "partial",
                "kalshi_api_status": summary.get("kalshi_api_status")
                or rate_limit.get("blocker")
                or rate_limit.get("status")
                or "COMPLETE",
                "request_pressure": {
                    "market_limit": DEFAULT_MARKET_PAGE_LIMIT,
                    "market_max_pages": 1,
                    "crypto_market_scan_limit": DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
                    "crypto_link_limit": DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
                    "snapshot_fetch_concurrency": DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
                },
            }
            watermark = _market_data_watermark(
                session,
                freshness_minutes=DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
            )
        except Exception as exc:  # noqa: BLE001 - operator report must capture the failed refresh.
            state = "REFRESH_FAILED"
            refresh_error = str(exc)
    payload = _with_metadata(
        {
            "phase": "3AK",
            "phase_version": PHASE3AK_VERSION,
            "mode": "BOUNDED_MARKET_DATA_REFRESH_COORDINATOR",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "live_or_demo_execution": False,
            "bounded": bounded,
            "max_duration_seconds": max_duration_seconds,
            "require_no_active_writer": require_no_active_writer,
            "run_refresh": run_refresh,
            "state": state,
            "active_writer": writer,
            "data_watermark": watermark,
            "refresh_started": refresh_started,
            "refresh_completed": refresh_completed,
            "refresh_error": refresh_error,
            "refresh_summary": refresh_summary,
            "db_writes": bool(refresh_started and refresh_completed),
            "next_action": (
                "Retry after db-writer-monitor reports safe_to_write=true."
                if blocked
                else ("Refresh not started by this status-only command." if not run_refresh else _refresh_next_action(state))
            ),
            "retry_after": "after active writer finishes" if blocked else None,
        },
        session=session,
        settings=resolved,
    )
    return payload


def write_market_data_refresh_status(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    bounded: bool = True,
    max_duration_seconds: int = 120,
    require_no_active_writer: bool = True,
    run_refresh: bool = True,
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
    settings: Settings | None = None,
) -> Phase3AKArtifactSet:
    payload = build_market_data_refresh_status(
        session,
        output_dir=output_dir,
        bounded=bounded,
        max_duration_seconds=max_duration_seconds,
        require_no_active_writer=require_no_active_writer,
        run_refresh=run_refresh,
        symbols=symbols,
        settings=settings,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "market_data_refresh_status.json"
    markdown_path = output_dir / "market_data_refresh_status.md"
    top_strip_path = output_dir / "top_strip_status.json"
    _write_json(json_path, payload)
    _write_json(top_strip_path, _top_strip_status(payload))
    markdown_path.write_text(_render_market_data_markdown(payload), encoding="utf-8")
    return Phase3AKArtifactSet(output_dir, json_path, markdown_path)


def write_phase_3ak_report(
    session: Session,
    *,
    output: Path = DEFAULT_REPORT_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
    symbols: str = DEFAULT_PHASE3AK_SYMBOLS,
) -> Phase3AKReportArtifactSet:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    window_artifacts, window_payload, window_source = _phase3ak_window_artifact_or_build(
        session,
        output_dir=output_dir,
        symbols=symbols,
        settings=resolved,
    )
    watch_artifacts, watch_payload, watch_source = _phase3ak_watch_artifact_or_build(
        session,
        output_dir=output_dir,
        symbols=symbols,
        settings=resolved,
    )
    market_payload, market_source = _phase3ak_market_artifact_or_status(
        session,
        output_dir=output_dir,
        symbols=symbols,
        settings=resolved,
    )
    _write_json(output_dir / "market_data_refresh_status.json", market_payload)
    _write_json(output_dir / "top_strip_status.json", _top_strip_status(market_payload))
    payload = _with_metadata(
        {
            "phase": "3AK",
            "phase_version": PHASE3AK_VERSION,
            "mode": "UNIFIED_MARKET_DATA_AND_CRYPTO_WINDOW_REPORT",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "live_or_demo_execution": False,
            "window_summary": window_payload.get("summary", {}),
            "readiness_funnel": window_payload.get("readiness_funnel", {}),
            "watch_status": {
                "runner_state": watch_payload.get("runner_state"),
                "primary_blocker": watch_payload.get("primary_blocker"),
                "watch_state": watch_payload.get("watch_state"),
            },
            "market_data_state": market_payload.get("state"),
            "top_strip_status": _top_strip_status(market_payload),
            "phase3w_consumable": True,
            "artifact_sources": {
                "crypto_window_sync": window_source,
                "crypto_watch_status": watch_source,
                "market_data_refresh_status": market_source,
            },
            "operator_actions": [
                window_payload.get("next_action"),
                watch_payload.get("next_action"),
                market_payload.get("next_action"),
            ],
            "artifacts": {
                "crypto_window_sync": str(window_artifacts.json_path),
                "crypto_windows": str(window_artifacts.rows_path),
                "crypto_watch_status": str(watch_artifacts.json_path),
                "market_data_refresh_status": str(output_dir / "market_data_refresh_status.json"),
                "top_strip_status": str(output_dir / "top_strip_status.json"),
            },
        },
        session=session,
        settings=resolved,
    )
    json_path = output_dir / "phase_3ak_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(json_path, payload)
    output.write_text(_render_phase3ak_markdown(payload), encoding="utf-8")
    return Phase3AKReportArtifactSet(json_path=json_path, markdown_path=output)


def _phase3ak_window_artifact_or_build(
    session: Session,
    *,
    output_dir: Path,
    symbols: str,
    settings: Settings,
) -> tuple[Phase3AKArtifactSet, dict[str, Any], str]:
    json_path = output_dir / "crypto_window_sync.json"
    rows_path = output_dir / "crypto_windows.json"
    markdown_path = output_dir / "crypto_window_sync.md"
    cached = _read_json(json_path)
    if cached and _artifact_age_seconds(cached) <= DEFAULT_PHASE3AK_REPORT_CACHE_SECONDS:
        return (
            Phase3AKArtifactSet(output_dir, json_path, markdown_path, rows_path),
            cached,
            "existing_artifact",
        )
    artifacts = write_crypto_window_sync_report(
        session,
        output_dir=output_dir,
        symbols=symbols,
        settings=settings,
    )
    source = "rebuilt_stale_artifact" if cached else "rebuilt_missing_artifact"
    return artifacts, _read_json(artifacts.json_path), source


def _phase3ak_watch_artifact_or_build(
    session: Session,
    *,
    output_dir: Path,
    symbols: str,
    settings: Settings,
) -> tuple[Phase3AKArtifactSet, dict[str, Any], str]:
    json_path = output_dir / "crypto_watch_status.json"
    markdown_path = output_dir / "crypto_watch_status.md"
    cached = _read_json(json_path)
    if cached and _artifact_age_seconds(cached) <= DEFAULT_PHASE3AK_REPORT_CACHE_SECONDS:
        return (
            Phase3AKArtifactSet(output_dir, json_path, markdown_path),
            cached,
            "existing_artifact",
        )
    artifacts = write_crypto_watch_status_report(
        session,
        output_dir=output_dir,
        symbols=symbols,
        settings=settings,
    )
    source = "rebuilt_stale_artifact" if cached else "rebuilt_missing_artifact"
    return artifacts, _read_json(artifacts.json_path), source


def _phase3ak_market_artifact_or_status(
    session: Session,
    *,
    output_dir: Path,
    symbols: str,
    settings: Settings,
) -> tuple[dict[str, Any], str]:
    json_path = output_dir / "market_data_refresh_status.json"
    cached = _read_json(json_path)
    if cached and _artifact_age_seconds(cached) <= 120:
        return cached, "existing_artifact"
    # Status-only when this report is called: do not start a second refresh job from a report.
    payload = build_market_data_refresh_status(
        session,
        output_dir=output_dir,
        require_no_active_writer=True,
        run_refresh=False,
        symbols=symbols,
        settings=settings,
    )
    if payload["state"] != "BLOCKED_BY_ACTIVE_WRITER":
        payload["state"] = "STATUS_ONLY_REFRESH_NOT_STARTED"
    return payload, "status_only_rebuilt_stale_or_missing_artifact"


def _artifact_age_seconds(payload: dict[str, Any]) -> int:
    generated_at = parse_datetime(payload.get("generated_at"))
    if generated_at is None:
        return 1_000_000_000
    return max(0, int((utc_now() - generated_at).total_seconds()))


def write_phase3ak_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ak"),
    limit: int | None = None,
    include_single_leg: bool = False,
    settings: Settings | None = None,
) -> Phase3AKReportArtifactSet:
    """Compatibility wrapper for the older Phase 3AK CLI slot.

    The current Phase 3AK implementation is the market-data/window orchestrator.
    The legacy sports provenance command can still call this function without
    breaking import-time CLI registration.
    """
    _ = (limit, include_single_leg)
    return write_phase_3ak_report(
        session,
        output=output_dir / "phase3ak_report.md",
        output_dir=output_dir,
        settings=settings,
    )


def multi_leg_learning_eligibility(session: Session, ticker: str) -> dict[str, Any]:
    legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker)
            .order_by(MarketLeg.leg_index)
        )
    )
    if len(legs) <= 1:
        return {
            "status": "NOT_MULTILEG",
            "eligible": True,
            "ticker": ticker,
            "leg_count": len(legs),
            "reason": "single_leg_or_unparsed",
        }
    return {
        "status": MULTILEG_REQUIRES_COMPONENT_PROVENANCE,
        "eligible": False,
        "ticker": ticker,
        "leg_count": len(legs),
        "component_categories": sorted({leg.category for leg in legs}),
        "reason": "multi_leg_component_provenance_required",
    }


def phase3ak_learning_rejection_reason(gate: dict[str, Any]) -> str:
    return str(gate.get("reason") or gate.get("status") or "phase3ak_learning_gate_block")


def build_multi_leg_component_provenance(
    session: Session,
    *,
    tickers: list[str] | None = None,
    include_single_leg: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    statement = select(MarketLeg)
    if tickers:
        statement = statement.where(MarketLeg.ticker.in_(tickers))
    statement = statement.order_by(MarketLeg.ticker, MarketLeg.leg_index)
    if limit is not None:
        statement = statement.limit(limit)
    grouped: dict[str, list[MarketLeg]] = {}
    for leg in session.scalars(statement):
        grouped.setdefault(leg.ticker, []).append(leg)
    if tickers:
        for ticker in tickers:
            grouped.setdefault(ticker, [])
    rows = []
    for ticker, legs in sorted(grouped.items()):
        is_multi_leg = len(legs) > 1
        if not include_single_leg and not is_multi_leg:
            continue
        component_counts = dict(sorted(Counter(leg.category for leg in legs).items()))
        eligible = not is_multi_leg
        rows.append(
            {
                "ticker": ticker,
                "is_multi_leg": is_multi_leg,
                "leg_count": len(legs),
                "component_status_counts": component_counts,
                "snapshot_status": {"status": "not_checked"},
                "learning_eligible": eligible,
                "learning_eligibility": NOT_MULTILEG
                if not is_multi_leg
                else MULTILEG_REQUIRES_COMPONENT_PROVENANCE,
                "blocking_reason": None
                if eligible
                else "multi_leg_component_provenance_required",
                "components": [
                    {
                        "leg_index": leg.leg_index,
                        "category": leg.category,
                        "operator": leg.operator,
                        "threshold_value": leg.threshold_value,
                        "entity_name": leg.entity_name,
                    }
                    for leg in legs
                ],
            }
        )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AK",
        "phase_version": PHASE3AK_VERSION,
        "mode": "COMPAT_MULTI_LEG_COMPONENT_PROVENANCE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "summary": {
            "rows": len(rows),
            "multi_leg_rows": sum(1 for row in rows if row["is_multi_leg"]),
            "learning_eligible_rows": sum(1 for row in rows if row["learning_eligible"]),
        },
        "rows": rows,
    }


def _crypto_markets(
    session: Session,
    *,
    symbols: list[str],
    scope: str,
    limit: int,
) -> list[Market]:
    prefixes = [
        prefix
        for symbol in symbols
        for prefix in ((supported_crypto_asset(symbol) or None).event_prefixes if supported_crypto_asset(symbol) else ())
    ]
    if not prefixes:
        return []
    filters = [Market.series_ticker.in_(prefixes)]
    filters.extend(Market.ticker.like(f"{prefix}%") for prefix in prefixes)
    statement = select(Market).where(or_(*filters)).order_by(desc(Market.last_seen_at)).limit(limit)
    markets = list(session.scalars(statement))
    if scope == "active":
        return [market for market in markets if market_status_bucket(market.status) == "active"]
    return markets


def _window_row(
    market: Market,
    *,
    link: CryptoMarketLink | None,
    legs: list[MarketLeg],
    snapshot: MarketSnapshot | None,
    forecast: Forecast | None,
    ranking: MarketRanking | None,
    sizing: PositionSizingDecisionLog | None,
    risk: AdvancedRiskDecisionLog | None,
    latest_features: dict[str, CryptoFeature],
    now: Any,
    freshness_minutes: int,
    settings: Settings,
) -> dict[str, Any]:
    terms = parse_crypto_market_terms(market, legs=legs)
    asset = _asset_for_market(market, terms=terms, link=link)
    close_time = _close_time(market, terms=terms)
    snapshot_age = _age_minutes(snapshot.captured_at, now) if snapshot is not None else None
    quote_fresh = snapshot_age is not None and snapshot_age <= Decimal(freshness_minutes)
    feature = latest_features.get(asset or "")
    feature_age = _age_minutes(feature.generated_at, now) if feature is not None else None
    feature_fresh = feature_age is not None and feature_age <= Decimal(freshness_minutes * 4)
    forecast_age = _age_minutes(forecast.forecasted_at, now) if forecast is not None else None
    forecast_valid = forecast_age is not None and forecast_age <= Decimal(freshness_minutes * 4)
    metrics = payout_metrics_from_ranking(ranking) if ranking is not None else None
    raw_ev = _raw_expected_value(forecast, ranking)
    book = (
        usable_bid_ask_book(
            decode_json(snapshot.raw_orderbook_json),
            side=str(ranking.best_side or ""),
            liquidity_score=ranking.liquidity_score,
            min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
            max_spread=settings.opportunity_max_spread,
        )
        if snapshot is not None and ranking is not None and ranking.best_side in {BUY_YES, BUY_NO}
        else None
    )
    ranking_spread = to_decimal(ranking.spread if ranking is not None else snapshot.spread if snapshot else None)
    spread = book.spread if book is not None and book.spread is not None else ranking_spread
    executable_ev = raw_ev - (spread or Decimal("0")) if raw_ev is not None else None
    phase3m_proposed_contracts = max(0, int(sizing.proposed_contracts)) if sizing is not None else 0
    phase3n_approved_contracts = (
        max(0, min(phase3m_proposed_contracts, int(risk.executed_contracts)))
        if risk is not None
        else 0
    )
    active_market = market_status_bucket(market.status) == "active"
    window_state = _window_state(
        market=market,
        terms_status=terms.status,
        close_time=close_time,
        quote_fresh=quote_fresh,
        active_market=active_market,
        now=now,
    )
    reason = _readiness_reason(
        active_market=active_market,
        linked=link is not None,
        terms_status=terms.status,
        window_state=window_state,
        quote_fresh=quote_fresh,
        feature_fresh=feature_fresh,
        forecast_valid=forecast_valid,
        raw_ev=raw_ev,
        executable_ev=executable_ev,
        spread=spread,
        book=book,
        liquidity_score=to_decimal(ranking.liquidity_score if ranking is not None else None),
        confidence_score=to_decimal(ranking.model_confidence_score if ranking is not None else None),
        opportunity_score=to_decimal(ranking.opportunity_score if ranking is not None else None),
        sizing=sizing,
        phase3m_proposed_contracts=phase3m_proposed_contracts,
        risk=risk,
        phase3n_approved_contracts=phase3n_approved_contracts,
        settings=settings,
    )
    component = terms.components[0] if terms.components else None
    strike, comparator = _strike_and_comparator(market.ticker, component)
    return {
        "ticker": market.ticker,
        "window_key": _window_key(market.ticker, asset, close_time),
        "asset": asset,
        "market_status": market.status,
        "active_market": active_market,
        "linked": link is not None,
        "parsed_status": terms.status,
        "window_state": window_state,
        "readiness_reason": reason,
        "title": market.title,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "strike": strike,
        "comparator": comparator,
        "observation_time": (
            terms.observation_time
            or (close_time.isoformat() if close_time is not None else None)
        ),
        "close_time": close_time.isoformat() if close_time is not None else None,
        "settlement_rule": terms.settlement_rules or market.rules_primary,
        "settlement_rule_version": "kalshi_source_terms_v1",
        "latest_snapshot_at": snapshot.captured_at.isoformat() if snapshot else None,
        "quote_age_minutes": decimal_to_str(snapshot_age),
        "quote_fresh": quote_fresh,
        "latest_feature_at": feature.generated_at.isoformat() if feature else None,
        "feature_age_minutes": decimal_to_str(feature_age),
        "feature_fresh": feature_fresh,
        "latest_forecast_at": forecast.forecasted_at.isoformat() if forecast else None,
        "forecast_age_minutes": decimal_to_str(forecast_age),
        "forecast_valid": forecast_valid,
        "raw_expected_value": decimal_to_str(raw_ev),
        "executable_expected_value": decimal_to_str(executable_ev),
        "ranking_expected_value": decimal_to_str(metrics.expected_value if metrics else None),
        "liquidity_score": ranking.liquidity_score if ranking else None,
        "ranking_spread": decimal_to_str(ranking_spread),
        "spread": decimal_to_str(spread),
        "confidence_score": ranking.model_confidence_score if ranking else None,
        "opportunity_score": ranking.opportunity_score if ranking else None,
        "phase3s_score_pass": (
            bool(ranking is not None)
            and (to_decimal(ranking.opportunity_score) or Decimal("0")) >= settings.opportunity_min_score
        ),
        "book_state": book.state if book is not None else None,
        "book_reason": book.reason if book is not None else "No eligible side/snapshot for executable book check.",
        "book_usable": book.usable if book is not None else False,
        "book_has_visible_bid_ask": book.has_visible_bid_ask if book is not None else False,
        "book_has_executable_depth": book.has_executable_depth if book is not None else False,
        "book_bid_price": decimal_to_str(book.bid_price if book is not None else None),
        "book_bid_depth": decimal_to_str(book.bid_depth if book is not None else None),
        "book_ask_price": decimal_to_str(book.ask_price if book is not None else None),
        "book_ask_depth": decimal_to_str(book.ask_depth if book is not None else None),
        "book_min_depth": decimal_to_str(book.min_depth if book is not None else None),
        "book_min_liquidity_score": decimal_to_str(book.min_liquidity_score if book is not None else None),
        "book_max_spread": decimal_to_str(book.max_spread if book is not None else None),
        "book_liquidity_pass": (
            bool(book is not None and book.has_executable_depth and book.liquidity_score is not None)
            and book.liquidity_score >= MIN_EXECUTABLE_LIQUIDITY_SCORE
        ),
        "book_spread_pass": (
            bool(book is not None and book.spread is not None)
            and Decimal("0") <= book.spread <= settings.opportunity_max_spread
        ),
        "phase3m_decision_id": sizing.id if sizing is not None else None,
        "phase3m_tier": sizing.tier if sizing is not None else None,
        "phase3m_proposed_contracts": phase3m_proposed_contracts,
        "phase3m_nonzero_size": sizing is not None and phase3m_proposed_contracts > 0,
        "phase3n_decision_id": risk.id if risk is not None else None,
        "phase3n_action": risk.action if risk is not None else None,
        "phase3n_approved_contracts": phase3n_approved_contracts,
        "phase3n_approved": _phase3n_approved(
            risk=risk,
            approved_contracts=phase3n_approved_contracts,
        ),
        "paper_ready": reason == "PAPER_READY",
    }


def _readiness_reason(
    *,
    active_market: bool,
    linked: bool,
    terms_status: str,
    window_state: str,
    quote_fresh: bool,
    feature_fresh: bool,
    forecast_valid: bool,
    raw_ev: Decimal | None,
    executable_ev: Decimal | None,
    spread: Decimal | None,
    book: Any | None,
    liquidity_score: Decimal | None,
    confidence_score: Decimal | None,
    opportunity_score: Decimal | None,
    sizing: PositionSizingDecisionLog | None,
    phase3m_proposed_contracts: int,
    risk: AdvancedRiskDecisionLog | None,
    phase3n_approved_contracts: int,
    settings: Settings,
) -> str:
    if not active_market:
        return "NO_ACTIVE_CRYPTO_MARKETS"
    if terms_status == AMBIGUOUS:
        return "ACTIVE_CRYPTO_MARKETS_NOT_PARSED"
    if terms_status != EXACT_LINK:
        return "ACTIVE_CRYPTO_MARKETS_NOT_PARSED"
    if not linked:
        return "ACTIVE_CRYPTO_MARKETS_NOT_LINKED"
    if window_state == "EXPIRED":
        return "EXPIRED_WINDOWS_ONLY"
    if window_state == "STALE" or not quote_fresh:
        return "QUOTE_STALE"
    if not feature_fresh:
        return "NO_FRESH_FEATURES"
    if not forecast_valid:
        return "NO_VALID_FORECAST"
    if raw_ev is None or raw_ev <= 0:
        return "NO_POSITIVE_RAW_EV"
    if book is None:
        return "LIQUIDITY_TOO_LOW"
    if book.state == "WIDE_SPREAD":
        return "SPREAD_TOO_WIDE"
    if (
        not book.has_visible_bid_ask
        or not book.has_executable_depth
        or book.state in {"NO_EXECUTABLE_BOOK", "THIN_BOOK"}
    ):
        return "LIQUIDITY_TOO_LOW"
    if liquidity_score is None or liquidity_score < MIN_EXECUTABLE_LIQUIDITY_SCORE:
        return "LIQUIDITY_TOO_LOW"
    if spread is not None and spread > settings.opportunity_max_spread:
        return "SPREAD_TOO_WIDE"
    if executable_ev is None or executable_ev <= 0:
        return "EV_LOST_TO_SPREAD"
    if confidence_score is not None and confidence_score < MIN_EXECUTABLE_CONFIDENCE_SCORE:
        return "CONFIDENCE_TOO_LOW"
    if opportunity_score is None or opportunity_score < settings.opportunity_min_score:
        return "PHASE_3S_SKIP"
    if sizing is None or phase3m_proposed_contracts <= 0:
        return "PHASE_3M_ZERO_SIZE"
    if not _phase3n_approved(risk=risk, approved_contracts=phase3n_approved_contracts):
        return "PHASE_3N_RISK_BLOCK"
    return "PAPER_READY"


def _phase3n_approved(
    *,
    risk: AdvancedRiskDecisionLog | None,
    approved_contracts: int,
) -> bool:
    if risk is None:
        return False
    if str(risk.action or "").upper() in {"BLOCK", "REJECT", "SKIP"}:
        return False
    return approved_contracts > 0


def _window_state(
    *,
    market: Market,
    terms_status: str,
    close_time: Any,
    quote_fresh: bool,
    active_market: bool,
    now: Any,
) -> str:
    status_bucket = market_status_bucket(market.status)
    if market.result or market.settlement_ts or status_bucket == "inactive":
        return "SETTLED" if market.result or market.settlement_ts else "EXPIRED"
    if terms_status == AMBIGUOUS:
        return "AMBIGUOUS"
    if terms_status == UNSUPPORTED or close_time is None:
        return "UNSUPPORTED"
    if close_time <= now:
        return "EXPIRED"
    if not active_market:
        return "UPCOMING"
    if not quote_fresh:
        return "STALE"
    return "ACTIVE"


def _window_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    active_markets = [row for row in rows if row["active_market"]]
    current_windows = [
        row
        for row in rows
        if row["window_state"] in {"ACTIVE", "STALE"}
        and row["parsed_status"] == EXACT_LINK
        and row["linked"]
    ]
    positive_ev_blockers = [
        row["readiness_reason"]
        for row in current_windows
        if (to_decimal(row["raw_expected_value"]) or Decimal("0")) > 0
        and row["readiness_reason"] != "PAPER_READY"
    ]
    return {
        "active_crypto_markets": len(active_markets),
        "parsed_markets": sum(1 for row in active_markets if row["parsed_status"] == EXACT_LINK),
        "linked_markets": sum(1 for row in active_markets if row["linked"]),
        "active_windows": len(current_windows),
        "active_fresh_windows": sum(1 for row in current_windows if row["window_state"] == "ACTIVE"),
        "active_stale_windows": sum(1 for row in current_windows if row["window_state"] == "STALE"),
        "upcoming_windows": sum(1 for row in rows if row["window_state"] == "UPCOMING"),
        "expired_windows": sum(1 for row in rows if row["window_state"] == "EXPIRED"),
        "settled_windows": sum(1 for row in rows if row["window_state"] == "SETTLED"),
        "unsupported_windows": sum(1 for row in rows if row["window_state"] == "UNSUPPORTED"),
        "ambiguous_windows": sum(1 for row in rows if row["window_state"] == "AMBIGUOUS"),
        "stale_windows": sum(1 for row in rows if row["window_state"] == "STALE"),
        "stale_quote_count": sum(1 for row in current_windows if not row["quote_fresh"]),
        "fresh_quote_count": sum(1 for row in current_windows if row["quote_fresh"]),
        "valid_features": sum(1 for row in current_windows if row["feature_fresh"]),
        "valid_forecasts": sum(1 for row in current_windows if row["forecast_valid"]),
        "positive_raw_ev": sum(
            1 for row in current_windows if (to_decimal(row["raw_expected_value"]) or Decimal("0")) > 0
        ),
        "positive_executable_ev": sum(
            1
            for row in current_windows
            if (to_decimal(row["executable_expected_value"]) or Decimal("0")) > 0
        ),
        "confidence_pass": sum(
            1
            for row in current_windows
            if (to_decimal(row["confidence_score"]) or Decimal("0")) >= MIN_EXECUTABLE_CONFIDENCE_SCORE
        ),
        "phase3s_score_pass": sum(
            1 for row in current_windows if row["phase3s_score_pass"]
        ),
        "liquidity_pass": sum(1 for row in current_windows if row["book_liquidity_pass"]),
        "spread_pass": sum(1 for row in current_windows if row["book_spread_pass"]),
        "usable_bid_ask_books": sum(1 for row in current_windows if row["book_usable"]),
        "phase3s_proceed": sum(
            1
            for row in current_windows
            if row["readiness_reason"] not in {"PHASE_3S_SKIP"}
            and row["phase3s_score_pass"]
        ),
        "phase3m_nonzero_size": sum(
            1 for row in current_windows if row["phase3m_nonzero_size"]
        ),
        "phase3n_approved": sum(
            1 for row in current_windows if row["phase3n_approved"]
        ),
        "paper_ready_opportunities": sum(1 for row in rows if row["paper_ready"]),
        "active_readiness_reason_counts": dict(
            sorted(Counter(row["readiness_reason"] for row in current_windows).items())
        ),
        "positive_ev_blocker_counts": dict(sorted(Counter(positive_ev_blockers).items())),
    }


def _readiness_funnel(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = _window_summary(rows)
    return {
        "active_crypto_markets": summary["active_crypto_markets"],
        "parsed_markets": summary["parsed_markets"],
        "linked_markets": summary["linked_markets"],
        "active_windows": summary["active_windows"],
        "fresh_quotes": summary["fresh_quote_count"],
        "valid_features": summary["valid_features"],
        "valid_forecasts": summary["valid_forecasts"],
        "positive_raw_ev": summary["positive_raw_ev"],
        "positive_executable_ev": summary["positive_executable_ev"],
        "confidence_pass": summary["confidence_pass"],
        "liquidity_pass": summary["liquidity_pass"],
        "spread_pass": summary["spread_pass"],
        "usable_bid_ask_books": summary["usable_bid_ask_books"],
        "phase3s_proceed": summary["phase3s_proceed"],
        "phase3m_nonzero_size": summary["phase3m_nonzero_size"],
        "phase3n_approved": summary["phase3n_approved"],
        "paper_ready_opportunities": summary["paper_ready_opportunities"],
    }


def _primary_blocker(summary: dict[str, Any]) -> str:
    if summary["active_crypto_markets"] == 0:
        return "NO_ACTIVE_CRYPTO_MARKETS"
    if summary["parsed_markets"] == 0:
        return "ACTIVE_CRYPTO_MARKETS_NOT_PARSED"
    if summary["linked_markets"] == 0:
        return "ACTIVE_CRYPTO_MARKETS_NOT_LINKED"
    if summary["active_windows"] == 0 and summary["stale_windows"] > 0:
        return "QUOTE_STALE"
    if summary["active_windows"] == 0 and summary["expired_windows"] > 0:
        return "EXPIRED_WINDOWS_ONLY"
    if summary["fresh_quote_count"] == 0:
        return "QUOTE_STALE"
    if summary["valid_features"] == 0:
        return "NO_FRESH_FEATURES"
    if summary["valid_forecasts"] == 0:
        return "NO_VALID_FORECAST"
    if summary["positive_raw_ev"] == 0:
        return "NO_POSITIVE_RAW_EV"
    if summary["liquidity_pass"] == 0:
        return "LIQUIDITY_TOO_LOW"
    if summary["spread_pass"] == 0:
        return "SPREAD_TOO_WIDE"
    if summary["positive_executable_ev"] == 0:
        return "EV_LOST_TO_SPREAD"
    if summary["paper_ready_opportunities"] == 0:
        positive_blocker = _first_summary_blocker(summary.get("positive_ev_blocker_counts") or {})
        if positive_blocker is not None:
            return positive_blocker
        active_blocker = _first_summary_blocker(summary.get("active_readiness_reason_counts") or {})
        if active_blocker is not None:
            return active_blocker
        return "PHASE_3N_RISK_BLOCK"
    return "PAPER_READY"


def _first_summary_blocker(counts: dict[str, Any]) -> str | None:
    for reason in (
        "LIQUIDITY_TOO_LOW",
        "SPREAD_TOO_WIDE",
        "EV_LOST_TO_SPREAD",
        "CONFIDENCE_TOO_LOW",
        "PHASE_3S_SKIP",
        "PHASE_3M_ZERO_SIZE",
        "PHASE_3N_RISK_BLOCK",
        "NO_POSITIVE_RAW_EV",
        "NO_VALID_FORECAST",
        "NO_FRESH_FEATURES",
        "QUOTE_STALE",
    ):
        if int(counts.get(reason) or 0) > 0:
            return reason
    return None


def _watch_primary_blocker(
    window_summary: dict[str, Any],
    watch_summary: dict[str, Any],
    *,
    heartbeat_state: str,
) -> str:
    if heartbeat_state in {"RUNNER_STALE", "RUNNING_CYCLE_OVERDUE", "STOPPED"}:
        return "WINDOW_SYNC_STALE"
    if int(watch_summary.get("paper_ready_candidates") or 0) > 0:
        return "PAPER_READY"
    return _primary_blocker(window_summary)


def _diagnosis(summary: dict[str, Any]) -> dict[str, bool]:
    return {
        "no_active_crypto_markets_exist": summary["active_crypto_markets"] == 0,
        "active_crypto_markets_exist_but_not_parsed": (
            summary["active_crypto_markets"] > 0 and summary["parsed_markets"] == 0
        ),
        "active_crypto_markets_exist_but_not_linked": (
            summary["active_crypto_markets"] > 0 and summary["linked_markets"] == 0
        ),
        "active_crypto_markets_exist_but_only_expired_windows_attached": (
            summary["active_crypto_markets"] > 0
            and summary["active_windows"] == 0
            and summary["expired_windows"] > 0
            and summary["stale_windows"] == 0
            and summary["upcoming_windows"] == 0
        ),
        "active_crypto_markets_exist_but_quote_data_stale": summary["stale_quote_count"] > 0,
        "active_crypto_markets_exist_but_no_positive_executable_ev": (
            summary["positive_executable_ev"] == 0
        ),
    }


def _latest_crypto_links(session: Session, tickers: list[str]) -> dict[str, CryptoMarketLink]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(CryptoMarketLink)
        .where(CryptoMarketLink.ticker.in_(tickers))
        .order_by(CryptoMarketLink.ticker, desc(CryptoMarketLink.detected_at), desc(CryptoMarketLink.id))
    )
    result: dict[str, CryptoMarketLink] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _legs_by_ticker(session: Session, tickers: list[str]) -> dict[str, list[MarketLeg]]:
    if not tickers:
        return {}
    grouped: dict[str, list[MarketLeg]] = {}
    rows = session.scalars(
        select(MarketLeg)
        .where(MarketLeg.ticker.in_(tickers))
        .order_by(MarketLeg.ticker, MarketLeg.leg_index)
    )
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)
    return grouped


def _latest_snapshots(session: Session, tickers: list[str]) -> dict[str, MarketSnapshot]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker.in_(tickers))
        .order_by(MarketSnapshot.ticker, desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
    )
    result: dict[str, MarketSnapshot] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _latest_forecasts(session: Session, tickers: list[str]) -> dict[str, Forecast]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(Forecast)
        .where(Forecast.ticker.in_(tickers), Forecast.model_name == MODEL_NAME)
        .order_by(Forecast.ticker, desc(Forecast.forecasted_at), desc(Forecast.id))
    )
    result: dict[str, Forecast] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _latest_rankings(session: Session, tickers: list[str]) -> dict[str, MarketRanking]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(MarketRanking)
        .where(MarketRanking.ticker.in_(tickers), MarketRanking.forecast_model == MODEL_NAME)
        .order_by(MarketRanking.ticker, desc(MarketRanking.ranked_at), desc(MarketRanking.id))
    )
    result: dict[str, MarketRanking] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _latest_sizing(session: Session, tickers: list[str]) -> dict[str, PositionSizingDecisionLog]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(PositionSizingDecisionLog)
        .where(
            PositionSizingDecisionLog.ticker.in_(tickers),
            PositionSizingDecisionLog.model_name == MODEL_NAME,
        )
        .order_by(
            PositionSizingDecisionLog.ticker,
            desc(PositionSizingDecisionLog.decision_timestamp),
            desc(PositionSizingDecisionLog.id),
        )
    )
    result: dict[str, PositionSizingDecisionLog] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _latest_risk(session: Session, tickers: list[str]) -> dict[str, AdvancedRiskDecisionLog]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .where(
            AdvancedRiskDecisionLog.ticker.in_(tickers),
            AdvancedRiskDecisionLog.model_id == MODEL_NAME,
        )
        .order_by(
            AdvancedRiskDecisionLog.ticker,
            desc(AdvancedRiskDecisionLog.created_at),
            desc(AdvancedRiskDecisionLog.id),
        )
    )
    result: dict[str, AdvancedRiskDecisionLog] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _latest_features(session: Session, symbols: list[str]) -> dict[str, CryptoFeature]:
    if not symbols:
        return {}
    rows = session.scalars(
        select(CryptoFeature)
        .where(CryptoFeature.symbol.in_(symbols))
        .order_by(CryptoFeature.symbol, desc(CryptoFeature.generated_at), desc(CryptoFeature.id))
    )
    result: dict[str, CryptoFeature] = {}
    for row in rows:
        result.setdefault(row.symbol, row)
    return result


def _asset_for_market(
    market: Market,
    *,
    terms: Any,
    link: CryptoMarketLink | None,
) -> str | None:
    if terms.symbol:
        return str(terms.symbol).split("+")[0]
    if link is not None and link.symbol:
        return link.symbol
    return symbol_from_event_ticker(market.series_ticker) or symbol_from_event_ticker(market.ticker)


def _close_time(market: Market, *, terms: Any) -> Any:
    return (
        parse_datetime(market.close_time)
        or parse_datetime(terms.expiration_time)
        or parse_datetime(terms.settlement_time)
        or crypto_ticker_close_time_utc(market.ticker)
    )


def _strike_and_comparator(ticker: str, component: Any | None) -> tuple[str | None, str | None]:
    strike = component.threshold_value if component is not None else None
    comparator = component.comparator if component is not None else None
    match = TARGET_PRICE_RE.search(str(ticker or "").upper())
    if match is not None:
        strike = strike or match.group("target")
        comparator = comparator or ("BELOW" if match.group("comparator") == "B" else "ABOVE")
    return strike, comparator


def _raw_expected_value(forecast: Forecast | None, ranking: MarketRanking | None) -> Decimal | None:
    if forecast is None or ranking is None:
        return None
    probability = to_decimal(forecast.yes_probability)
    price = to_decimal(ranking.best_price)
    if probability is None or price is None:
        return None
    side_probability = Decimal("1") - probability if ranking.best_side == BUY_NO else probability
    return side_probability - price


def _age_minutes(value: Any, now: Any) -> Decimal | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return Decimal(str(max(0, (now - parsed).total_seconds()) / 60))


def _market_data_watermark(session: Session, *, freshness_minutes: int) -> dict[str, Any]:
    latest = session.scalar(select(MarketSnapshot).order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)).limit(1))
    now = utc_now()
    age = _age_minutes(latest.captured_at, now) if latest is not None else None
    state = (
        "MISSING"
        if latest is None
        else ("FRESH" if age is not None and age <= Decimal(freshness_minutes) else "STALE")
    )
    return {
        "state": state,
        "latest_market_snapshot_at": latest.captured_at.isoformat() if latest else None,
        "age_minutes": decimal_to_str(age),
        "freshness_threshold_minutes": str(freshness_minutes),
    }


def _writer_status(*, settings: Settings) -> dict[str, Any]:
    monitor = db_writer_monitor(settings=settings)
    process_guard = _active_writer_process()
    active = bool(monitor.get("current_writer_pid")) or process_guard is not None
    command = monitor.get("current_writer_command") or (process_guard or {}).get("command")
    pid = monitor.get("current_writer_pid") or (process_guard or {}).get("pid")
    return {
        "active_writer": active,
        "writer_name": _writer_name(str(command or "")),
        "pid": pid,
        "writer_command": command,
        "safe_to_write": bool(monitor.get("safe_to_start_write")) and process_guard is None,
        "raw_status": (
            monitor.get("status")
            if monitor.get("current_writer_pid")
            else ("PROCESS_ACTIVE_NO_DB_LOCK" if process_guard else monitor.get("status"))
        ),
        "recommended_action": (
            f"Wait for writer process pid {pid} to finish, then rerun db-writer-monitor."
            if process_guard
            else monitor.get("recommended_next_action")
        ),
        "long_job_status": monitor.get("long_job_status"),
    }


def _active_writer_process() -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    current_pid = os.getpid()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid or "python" not in command:
            continue
        if any(marker in command for marker in WRITER_PROCESS_MARKERS):
            return {"pid": pid, "command": command}
    return None


def _writer_name(command: str) -> str | None:
    lowered = command.lower()
    if "phase3bc-r5" in lowered or "crypto" in lowered:
        return "crypto_watcher"
    if "market-data-refresh" in lowered:
        return "market_data_refresh"
    return "unknown_writer" if command else None


def _top_strip_status(refresh_payload: dict[str, Any]) -> dict[str, Any]:
    watermark = refresh_payload.get("data_watermark") or {}
    writer = refresh_payload.get("active_writer") or {}
    return {
        "state": refresh_payload.get("state"),
        "market_data_state": watermark.get("state"),
        "data_watermark": watermark.get("latest_market_snapshot_at"),
        "staleness_age_minutes": watermark.get("age_minutes"),
        "freshness_threshold_minutes": watermark.get("freshness_threshold_minutes"),
        "active_writer": bool(writer.get("active_writer")),
        "active_writer_name": writer.get("writer_name"),
        "active_writer_pid": writer.get("pid"),
        "blocked_reason": "BLOCKED_BY_ACTIVE_WRITER"
        if refresh_payload.get("state") == "BLOCKED_BY_ACTIVE_WRITER"
        else None,
        "next_retry_time": refresh_payload.get("retry_after"),
        "database_fingerprint": refresh_payload.get("database_fingerprint"),
        "environment_mode": "paper/demo/read-only",
    }


def _heartbeat_state(
    *,
    runner_status: str,
    runner_running: bool,
    latest_age_seconds: int | None,
    freshness_minutes: int,
    stale_report: bool = False,
    seconds_until_timeout: int | None = None,
) -> str:
    if not runner_running:
        return "STOPPED"
    if latest_age_seconds is None:
        return "RUNNER_STALE"
    if latest_age_seconds > freshness_minutes * 60 or stale_report:
        if runner_status == "RUNNING" and seconds_until_timeout is not None and seconds_until_timeout >= 0:
            return "RUNNING_CYCLE_OVERDUE"
        return "RUNNER_STALE"
    if runner_status == "RUNNING":
        return "RUNNING"
    return runner_status


def _watch_next_action(blocker: str, *, heartbeat_state: str) -> str:
    if blocker == "WINDOW_SYNC_STALE" and heartbeat_state == "RUNNING_CYCLE_OVERDUE":
        return (
            "Crypto watcher is active, but the last completed cycle is overdue; "
            "wait for the in-flight cycle or inspect the slow stage before restarting."
        )
    return _next_action_for_blocker(blocker)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _watch_state_for_blocker(blocker: str) -> str:
    return {
        "PAPER_READY": "PAPER_READY",
        "WINDOW_SYNC_STALE": "RUNNER_STALE",
        "NO_ACTIVE_CRYPTO_MARKETS": "NO_ACTIVE_CRYPTO_MARKETS",
        "EXPIRED_WINDOWS_ONLY": "EXPIRED_WINDOWS_ONLY",
        "QUOTE_STALE": "QUOTE_STALE",
        "NO_POSITIVE_RAW_EV": "WAITING_FOR_POSITIVE_EV",
        "EV_LOST_TO_SPREAD": "WAITING_FOR_EXECUTION_QUALITY",
        "LIQUIDITY_TOO_LOW": "WAITING_FOR_EXECUTION_QUALITY",
        "SPREAD_TOO_WIDE": "WAITING_FOR_EXECUTION_QUALITY",
        "PHASE_3S_SKIP": "PHASE_3S_SKIP",
    }.get(blocker, blocker)


def _next_action_for_blocker(blocker: str) -> str:
    return {
        "PAPER_READY": "Paper-ready candidates exist; keep paper-only and inspect Phase 3M/3N evidence.",
        "NO_ACTIVE_CRYPTO_MARKETS": "Refresh active Kalshi BTC/ETH markets before forecasting.",
        "ACTIVE_CRYPTO_MARKETS_NOT_PARSED": "Run/repair crypto market parser before links or forecasts.",
        "ACTIVE_CRYPTO_MARKETS_NOT_LINKED": "Run/repair crypto market linker for active BTC/ETH tickers.",
        "EXPIRED_WINDOWS_ONLY": "Run crypto-window-sync after refreshing active markets; expired rows stay historical.",
        "WINDOW_SYNC_STALE": "Restart or repair the crypto watcher heartbeat before trusting freshness status.",
        "QUOTE_STALE": "Refresh exact active-window order books after the writer lane is clear.",
        "NO_FRESH_FEATURES": "Build fresh crypto features for active BTC/ETH windows.",
        "NO_VALID_FORECAST": "Run crypto_v2 forecasts against current active-window snapshots.",
        "NO_POSITIVE_RAW_EV": "Keep watching; current active windows do not have positive model EV.",
        "EV_LOST_TO_SPREAD": "Wait for tighter executable spread; do not force a paper trade.",
        "LIQUIDITY_TOO_LOW": "Wait for executable book depth/liquidity.",
        "SPREAD_TOO_WIDE": "Wait for spread to tighten below threshold.",
        "CONFIDENCE_TOO_LOW": "Wait for model confidence to clear the paper gate.",
        "PHASE_3S_SKIP": "Keep paper-only row skipped until Phase 3S opportunity policy allows proceed.",
        "PHASE_3N_RISK_BLOCK": "Respect risk block; inspect Phase 3N logs.",
        "BLOCKED_BY_ACTIVE_WRITER": "Retry after db-writer-monitor reports safe_to_write=true.",
    }.get(blocker, "Continue bounded crypto watch and inspect the next report.")


def _refresh_next_action(state: str) -> str:
    if state == "REFRESH_COMPLETED":
        return "Rerun crypto-window-sync and crypto-watch-status to update UI evidence."
    if state == "RATE_LIMITED_KALSHI_API":
        return "Wait for Kalshi backoff, then rerun bounded market-data-refresh; keep paper-ready blocked while data is partial."
    if state == "REFRESH_FAILED":
        return "Inspect market_data_refresh_status refresh_error before retrying."
    return "Ready to run bounded refresh."


def _parse_symbols(symbols: str) -> list[str]:
    return [part.strip().upper() for part in symbols.split(",") if part.strip()]


def _window_key(ticker: str, asset: str | None, close_time: Any) -> str:
    return f"{asset or 'UNKNOWN'}:{close_time.isoformat() if close_time else 'UNKNOWN'}:{ticker}"


def _duplicate_count(values: list[str]) -> int:
    counts = Counter(values)
    return sum(count - 1 for count in counts.values() if count > 1)


def _database_fingerprint(settings: Settings) -> dict[str, Any]:
    db_url = database_url_from_settings(settings)
    sqlite_path = sqlite_path_from_url(db_url)
    if sqlite_path is None:
        return {"kind": "non_sqlite", "path": None}
    path = sqlite_path.expanduser().resolve()
    if not path.exists():
        return {"kind": "missing", "path": str(path)}
    stat = path.stat()
    return {
        "kind": "sqlite_file_stat",
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _with_metadata(payload: dict[str, Any], *, session: Session, settings: Settings) -> dict[str, Any]:
    payload.setdefault("generated_at", utc_now().isoformat())
    payload.setdefault("command", " ".join(str(part) for part in sys.argv if part))
    payload.setdefault("command_args", sys.argv[1:])
    payload.setdefault("database_fingerprint", _database_fingerprint(settings))
    payload.setdefault("data_watermark", _market_data_watermark(session, freshness_minutes=DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES))
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _render_window_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AK Crypto Window Sync",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        f"- Primary blocker: `{payload['primary_blocker']}`",
        f"- Active crypto markets: `{summary['active_crypto_markets']}`",
        f"- Active windows: `{summary['active_windows']}`",
        f"- Stale windows: `{summary['stale_windows']}`",
        f"- Expired windows: `{summary['expired_windows']}`",
        f"- Paper-ready opportunities: `{summary['paper_ready_opportunities']}`",
        "",
        f"Next action: {payload['next_action']}",
    ]
    return "\n".join(lines)


def _render_watch_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AK Crypto Watch Status",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Runner state: `{payload['runner_state']}`",
        f"- Watch state: `{payload['watch_state']}`",
        f"- Primary blocker: `{payload['primary_blocker']}`",
        f"- Active windows: `{payload['window_summary']['active_windows']}`",
        f"- Stale quotes: `{payload['window_summary']['stale_quote_count']}`",
        f"- Paper-ready: `{payload['readiness_funnel']['paper_ready_opportunities']}`",
        "",
        f"Next action: {payload['next_action']}",
    ]
    return "\n".join(lines)


def _render_market_data_markdown(payload: dict[str, Any]) -> str:
    watermark = payload["data_watermark"]
    return "\n".join(
        [
            "# Phase 3AK Market Data Refresh",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- State: `{payload['state']}`",
            f"- Market data: `{watermark['state']}`",
            f"- Watermark: `{watermark.get('latest_market_snapshot_at')}`",
            f"- Active writer: `{payload['active_writer'].get('writer_name')}`",
            "",
            f"Next action: {payload['next_action']}",
        ]
    )


def _render_phase3ak_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AK Market Data and Crypto Window Report",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Safety: `{payload['paper_only_safety']}`",
        f"- Live trading: `disabled`",
        f"- Watch blocker: `{payload['watch_status']['primary_blocker']}`",
        f"- Runner state: `{payload['watch_status']['runner_state']}`",
        f"- Market data state: `{payload['market_data_state']}`",
        f"- Active windows: `{payload['window_summary'].get('active_windows')}`",
        f"- Paper-ready: `{payload['readiness_funnel'].get('paper_ready_opportunities')}`",
        "",
        "## Operator Actions",
        "",
    ]
    for action in payload["operator_actions"]:
        if action:
            lines.append(f"- {action}")
    return "\n".join(lines)
