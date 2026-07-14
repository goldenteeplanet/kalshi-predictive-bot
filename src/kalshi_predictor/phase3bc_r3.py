from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import (
    DEFAULT_CRYPTO_SYMBOLS,
    SUPPORTED_CRYPTO_ASSETS,
    symbol_from_event_ticker,
)
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.ingestion import ingest_crypto_quotes
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.repository import get_latest_crypto_price, parse_symbols
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.repositories import insert_market_snapshot, upsert_market
from kalshi_predictor.data.schema import MarketRanking, MarketSnapshot
from kalshi_predictor.forecasting.registry import (
    latest_snapshots_for_forecasts,
    latest_snapshots_for_model,
    run_forecast_models,
)
from kalshi_predictor.jobs.collect_once import CollectOnceSummary, collect_once
from kalshi_predictor.kalshi.client import (
    RATE_LIMITED_ABORTED,
    RATE_LIMITED_PARTIAL,
    RATE_LIMITED_RETRY_EXHAUSTED,
    KalshiClient,
    KalshiClientError,
    KalshiRetryError,
)
from kalshi_predictor.opportunities.reports import generate_opportunities_report
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ar import write_phase3ar_report
from kalshi_predictor.phase3at import CURRENT_PAPER_SCAN, current_crypto_opportunity_scope
from kalshi_predictor.phase3bc import write_phase3bc_crypto_clean_opportunity_report
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BC_R3_VERSION = "phase3bc_r3_active_crypto_refresh_liquidity_first"
MODEL_NAME = "crypto_v2"
DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES = 15
DEFAULT_CRYPTO_SERIES_TICKERS = ",".join(
    asset.event_prefixes[0] for asset in SUPPORTED_CRYPTO_ASSETS if asset.event_prefixes
)
DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT = 40
DEFAULT_NEAR_MONEY_WINDOW_LIMIT = 20
DEFAULT_SNAPSHOT_FETCH_CONCURRENCY = 2
DEFAULT_MARKET_PAGE_LIMIT = 150
DEFAULT_CRYPTO_MARKET_SCAN_LIMIT = 2500
DEFAULT_CRYPTO_LINK_SCAN_LIMIT = 500
CRYPTO_TARGET_PRICE_RE = re.compile(r"-(?:B|T)(?P<target>\d+(?:\.\d+)?)(?:$|-)")


@dataclass(frozen=True)
class Phase3BCR3ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


def write_phase3bc_r3_active_crypto_refresh_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    settings: Settings | None = None,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: str = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: str = "coinbase",
    refresh_open_markets: bool = False,
    external_crypto_ingest: bool = True,
    repair_snapshots: bool = True,
    market_limit: int = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: int = 1,
    crypto_market_scan_limit: int = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: int = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: int = 1000,
    opportunity_limit: int = 150,
    phase3bc_limit: int = 1000,
    cadence_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    forecast_current_windows_only: bool = False,
    generate_opportunity_report: bool = True,
    near_money_only: bool = False,
    near_money_per_symbol_limit: int = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: int = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: int = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
) -> Phase3BCR3ArtifactSet:
    """Run one bounded paper-only crypto refresh cycle and report readiness."""
    resolved = settings or get_settings()
    requested_symbols = parse_symbols(symbols)
    requested_series = _parse_csv(crypto_series_tickers)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = utc_now()
    stage_timer = _StageTimer(output_dir)
    active_watch_blocker = (
        _active_crypto_watch_blocker() if refresh_open_markets else None
    )
    if active_watch_blocker is not None:
        return _write_concurrent_refresh_blocked_report(
            output_dir=output_dir,
            generated_at=generated_at,
            cadence_minutes=cadence_minutes,
            lock_info=active_watch_blocker,
            blocker="ACTIVE_CRYPTO_WATCH_ALREADY_RUNNING",
        )
    refresh_lock_path, refresh_lock_blocker = (
        _acquire_refresh_lock(output_dir) if refresh_open_markets else (None, None)
    )
    if refresh_lock_blocker is not None:
        return _write_concurrent_refresh_blocked_report(
            output_dir=output_dir,
            generated_at=generated_at,
            cadence_minutes=cadence_minutes,
            lock_info=refresh_lock_blocker,
            blocker="ACTIVE_KALSHI_REFRESH_ALREADY_RUNNING",
        )

    ingest_summary = None
    if external_crypto_ingest and refresh_open_markets and near_money_only:
        stage_timer.mark("ingest_crypto_quotes")
        ingest_summary = ingest_crypto_quotes(session, symbols=requested_symbols, source=source)
    collect_skipped_reason = None
    if refresh_open_markets and _recent_complete_refresh_exists(
        output_dir,
        cadence_minutes=cadence_minutes,
    ):
        stage_timer.mark("collect_crypto_series_skipped_recent_cache")
        collect_skipped_reason = "RECENT_COMPLETE_REFRESH_CACHE"
        collect_summaries = []
    else:
        stage_timer.mark("collect_crypto_series")
        collect_summaries = (
            _collect_crypto_series(
                session,
                series_tickers=requested_series,
                symbols=requested_symbols,
                limit=market_limit,
                max_pages=market_max_pages,
                near_money_only=near_money_only,
                near_money_per_symbol_limit=near_money_per_symbol_limit,
                near_money_window_limit=near_money_window_limit,
                snapshot_fetch_concurrency=snapshot_fetch_concurrency,
            )
            if refresh_open_markets
            else []
        )
    effective_repair_snapshots = repair_snapshots and collect_skipped_reason is None
    if ingest_summary is None:
        stage_timer.mark("ingest_crypto_quotes")
        ingest_summary = (
            ingest_crypto_quotes(session, symbols=requested_symbols, source=source)
            if external_crypto_ingest
            else None
    )
    stage_timer.mark("build_crypto_features")
    feature_summary = build_crypto_features(session, symbols=requested_symbols)
    stage_timer.mark("link_crypto_markets")
    link_tickers = _collected_snapshot_tickers(collect_summaries) if near_money_only else None
    link_summary = link_crypto_markets(
        session,
        limit=crypto_market_scan_limit,
        tickers=link_tickers,
    )
    phase3ar_tickers = link_tickers if near_money_only else None
    stage_timer.mark("phase3ar_snapshot_repair")
    phase3ar_artifacts = write_phase3ar_report(
        session,
        output_dir=output_dir / "phase3ar",
        settings=resolved,
        limit=crypto_link_limit,
        repair_snapshots=effective_repair_snapshots,
        tickers=phase3ar_tickers,
    )
    stage_timer.mark("latest_snapshots_for_forecast")
    snapshots, forecast_input_scope = _forecast_snapshot_candidates(
        session,
        model_name=MODEL_NAME,
        limit=forecast_limit,
        near_money_only=near_money_only,
        link_tickers=link_tickers,
    )
    forecast_snapshots, forecast_scope = _forecast_snapshots_for_scope(
        snapshots,
        current_windows_only=forecast_current_windows_only,
    )
    stage_timer.mark("run_crypto_forecast")
    forecast_summary = run_forecast_models(
        session,
        model_name=MODEL_NAME,
        snapshots=forecast_snapshots,
    )
    stage_timer.mark("generate_opportunities")
    opportunities_path = output_dir / "opportunities_crypto_v2.md"
    current_scope = current_crypto_opportunity_scope(
        session,
        settings=resolved,
        limit=crypto_link_limit,
    )
    if generate_opportunity_report:
        opportunities_path, opportunity_summary = generate_opportunities_report(
            session,
            model_name=MODEL_NAME,
            limit=opportunity_limit,
            output_path=opportunities_path,
            settings=resolved,
            ticker_scope=current_scope.get("paper_scan_tickers") or current_scope["tickers"],
            scan_mode=CURRENT_PAPER_SCAN,
        )
    else:
        opportunity_summary = _SkippedOpportunitySummary()
        opportunities_path.write_text(
            "# Phase 3BC-R3 Crypto Opportunity Report\n\n"
            "Skipped in fast watch mode; Phase 3BC-R7 repairs current-window rankings.\n",
            encoding="utf-8",
        )
    stage_timer.mark("phase3bc_router")
    phase3bc_artifacts = write_phase3bc_crypto_clean_opportunity_report(
        session,
        output_dir=phase3bc_output_dir,
        settings=resolved,
        limit=phase3bc_limit,
    )

    stage_timer.mark("build_report_payload")
    phase3ar_payload = _read_json(phase3ar_artifacts.json_path)
    phase3bc_payload = _read_json(phase3bc_artifacts.json_path)
    payload = _payload(
        generated_at=generated_at,
        cadence_minutes=cadence_minutes,
        symbols=requested_symbols,
        crypto_series_tickers=requested_series,
        collect_summaries=collect_summaries,
        ingest_summary=ingest_summary,
        feature_summary=feature_summary,
        link_summary=link_summary,
        phase3ar_payload=phase3ar_payload,
        forecast_summary=forecast_summary,
        forecast_scope=forecast_scope,
        opportunity_summary=opportunity_summary,
        opportunities_path=opportunities_path,
        phase3bc_payload=phase3bc_payload,
        phase3ar_artifacts=phase3ar_artifacts,
        phase3bc_artifacts=phase3bc_artifacts,
        external_crypto_ingest=external_crypto_ingest,
        refresh_open_markets=refresh_open_markets,
        repair_snapshots=effective_repair_snapshots,
        collect_skipped_reason=collect_skipped_reason,
        forecast_current_windows_only=forecast_current_windows_only,
        forecast_input_scope=forecast_input_scope,
        generate_opportunity_report=generate_opportunity_report,
        near_money_only=near_money_only,
        near_money_per_symbol_limit=near_money_per_symbol_limit,
        near_money_window_limit=near_money_window_limit,
        snapshot_fetch_concurrency=snapshot_fetch_concurrency,
        crypto_market_scan_limit=crypto_market_scan_limit,
        link_ticker_count=len(link_tickers) if link_tickers is not None else None,
        phase3ar_ticker_count=len(phase3ar_tickers) if phase3ar_tickers is not None else None,
        current_crypto_scope=current_scope,
        stage_timings=stage_timer.timings,
    )
    json_path = output_dir / "phase3bc_r3_active_crypto_refresh.json"
    markdown_path = output_dir / "phase3bc_r3_active_crypto_refresh.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    stage_timer.mark("complete")
    _release_refresh_lock(refresh_lock_path)
    return Phase3BCR3ArtifactSet(output_dir, json_path, markdown_path)


class _StageTimer:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.timings: list[dict[str, Any]] = []
        self._current_stage: str | None = None
        self._current_started_at: Any | None = None

    def mark(self, stage: str) -> None:
        now = utc_now()
        if self._current_stage is not None and self._current_started_at is not None:
            self.timings.append(
                {
                    "stage": self._current_stage,
                    "started_at": self._current_started_at.isoformat(),
                    "completed_at": now.isoformat(),
                    "duration_seconds": round(
                        (now - self._current_started_at).total_seconds(),
                        3,
                    ),
                }
            )
        self._current_stage = stage
        self._current_started_at = now
        _write_stage_marker(self.output_dir, stage, stage_timings=self.timings)


def _write_stage_marker(
    output_dir: Path,
    stage: str,
    *,
    stage_timings: list[dict[str, Any]] | None = None,
) -> None:
    payload = {
        "generated_at": utc_now().isoformat(),
        "phase": "3BC-R3",
        "stage": stage,
        "completed_stage_timings": stage_timings or [],
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "phase3bc_r3_stage.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"Phase 3BC-R3 stage: {stage}", flush=True)


def _acquire_refresh_lock(output_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    lock_path = output_dir / "kalshi_api_refresh.lock"
    lock_payload = {
        "pid": os.getpid(),
        "created_at": utc_now().isoformat(),
        "phase": "3BC-R3",
        "purpose": "single Kalshi public API crypto refresh guard",
    }
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            blocker = _read_refresh_lock(lock_path)
            pid = _lock_pid(blocker)
            if pid is not None and not _pid_is_running(pid):
                try:
                    lock_path.unlink()
                    continue
                except OSError:
                    pass
            return None, blocker or {"lock_path": str(lock_path)}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(lock_payload, handle, indent=2, sort_keys=True)
        return lock_path, None


def _active_crypto_watch_blocker(
    output_dir: Path = Path("reports/phase3bc_r5"),
) -> dict[str, Any] | None:
    pid_path = output_dir / "phase3bc_r5_unattended_job.pid"
    pid = _read_pid_file(pid_path)
    if pid is None or pid == os.getpid():
        return None
    if not _pid_is_running(pid):
        return None
    command = _pid_command_line(pid)
    if "phase3bc-r5-crypto-freshness-watch" not in command:
        return None
    return {
        "pid": pid,
        "pid_path": str(pid_path),
        "command": command,
        "reason": "separate Phase 3BC-R5 crypto watch is already active",
    }


def _read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return None


def _pid_command_line(pid: int) -> str:
    proc_path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_path.read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")


def _release_refresh_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def _read_refresh_lock(lock_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["lock_path"] = str(lock_path)
    return payload


def _lock_pid(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    try:
        return int(payload.get("pid") or 0) or None
    except (TypeError, ValueError):
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_concurrent_refresh_blocked_report(
    *,
    output_dir: Path,
    generated_at: datetime,
    cadence_minutes: int,
    lock_info: dict[str, Any],
    blocker: str,
) -> Phase3BCR3ArtifactSet:
    json_path = output_dir / "phase3bc_r3_active_crypto_refresh.json"
    markdown_path = output_dir / "phase3bc_r3_active_crypto_refresh.md"
    reason = (
        "Another bounded Kalshi crypto refresh is already active."
        if blocker == "ACTIVE_KALSHI_REFRESH_ALREADY_RUNNING"
        else "Another guarded crypto watcher is already active."
    )
    payload = {
        "generated_at": generated_at.isoformat(),
        "phase": "3BC-R3",
        "phase_version": PHASE3BC_R3_VERSION,
        "mode": "PAPER_ONLY_ACTIVE_CRYPTO_REFRESH_AND_RANKING_FRESHNESS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "symbols": [],
        "crypto_series_tickers": [],
        "cadence": {
            "target_minutes": cadence_minutes,
            "reason": reason,
        },
        "options": {
            "refresh_open_markets": True,
            "collect_skipped_reason": blocker,
        },
        "rate_limit": {
            "status": blocker,
            "blocker": blocker,
            "rate_limited": False,
            "data_complete": False,
            "data_completeness": "unknown",
            "affected_stages": ["collect_crypto_series"],
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "retry_exhausted_count": 0,
            "total_sleep_seconds": 0.0,
            "rows_fetched_before_limit": 0,
            "top_endpoint": None,
            "endpoints": [],
            "lock_info": lock_info,
        },
        "summary": {
            "kalshi_api_status": blocker,
            "data_complete": False,
            "data_completeness": "unknown",
            "paper_ready_blocked_by_rate_limit": True,
            "crypto_series_refreshes": [],
            "phase3bc_paper_ready_candidates": 0,
            "phase3bc_raw_paper_ready_candidates": 0,
            "phase3bc_main_blocker": blocker,
        },
        "freshness": {},
        "stage_timings": [],
        "stage_duration_seconds": {},
        "reports": {},
        "recommended_next_action": (
            "A Kalshi crypto refresh/watch is already active; wait for it to finish "
            "instead of starting a duplicate request stream."
        ),
        "next_commands": ["kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"],
    }
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3BCR3ArtifactSet(output_dir, json_path, markdown_path)


@dataclass(frozen=True)
class _SkippedOpportunitySummary:
    markets_scanned: int = 0
    rankings_inserted: int = 0
    opportunities_detected: int = 0


def _payload(
    *,
    generated_at: Any,
    cadence_minutes: int,
    symbols: list[str],
    crypto_series_tickers: list[str],
    collect_summaries: list[CollectOnceSummary],
    ingest_summary: Any,
    feature_summary: Any,
    link_summary: Any,
    phase3ar_payload: dict[str, Any],
    forecast_summary: Any,
    forecast_scope: dict[str, Any],
    opportunity_summary: Any,
    opportunities_path: Path,
    phase3bc_payload: dict[str, Any],
    phase3ar_artifacts: Any,
    phase3bc_artifacts: Any,
    external_crypto_ingest: bool,
    refresh_open_markets: bool,
    repair_snapshots: bool,
    collect_skipped_reason: str | None,
    forecast_current_windows_only: bool,
    forecast_input_scope: str,
    generate_opportunity_report: bool,
    near_money_only: bool,
    near_money_per_symbol_limit: int,
    near_money_window_limit: int,
    snapshot_fetch_concurrency: int,
    crypto_market_scan_limit: int,
    link_ticker_count: int | None,
    phase3ar_ticker_count: int | None,
    current_crypto_scope: dict[str, Any],
    stage_timings: list[dict[str, Any]],
) -> dict[str, Any]:
    freshness = _freshness_summary(phase3bc_payload, cadence_minutes=cadence_minutes)
    phase3bc_summary = phase3bc_payload.get("summary", {})
    phase3ar_summary = phase3ar_payload.get("summary", {})
    per_symbol_snapshot_counts = _aggregate_symbol_counts(collect_summaries)
    per_symbol_liquidity_first_counts = _aggregate_liquidity_first_counts(
        collect_summaries
    )
    rate_limit = _rate_limit_summary(collect_summaries)
    rate_limit_blocked = bool(rate_limit.get("rate_limited"))
    raw_paper_ready_candidates = phase3bc_summary.get("paper_ready_candidates", 0)
    phase3bc_paper_ready_candidates = (
        0 if rate_limit_blocked else raw_paper_ready_candidates
    )
    phase3bc_main_blocker = (
        "RATE_LIMITED_KALSHI_API"
        if rate_limit_blocked
        else phase3bc_summary.get("main_blocker")
    )
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BC-R3",
        "phase_version": PHASE3BC_R3_VERSION,
        "mode": "PAPER_ONLY_ACTIVE_CRYPTO_REFRESH_AND_RANKING_FRESHNESS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "symbols": symbols,
        "crypto_series_tickers": crypto_series_tickers,
        "cadence": {
            "target_minutes": cadence_minutes,
            "reason": (
                "Kalshi crypto markets and actionable quotes should be refreshed every "
                "15 minutes."
            ),
        },
        "options": {
            "external_crypto_ingest": external_crypto_ingest,
            "refresh_open_markets": refresh_open_markets,
            "repair_snapshots": repair_snapshots,
            "collect_skipped_reason": collect_skipped_reason,
            "forecast_current_windows_only": forecast_current_windows_only,
            "forecast_input_scope": forecast_input_scope,
            "generate_opportunity_report": generate_opportunity_report,
            "near_money_only": near_money_only,
            "near_money_per_symbol_limit": near_money_per_symbol_limit,
            "near_money_window_limit": near_money_window_limit,
            "snapshot_fetch_concurrency": snapshot_fetch_concurrency,
            "crypto_market_scan_limit": crypto_market_scan_limit,
            "linker_scope": (
                "NEAR_MONEY_SNAPSHOT_TICKERS"
                if near_money_only
                else "FULL_MARKET_SCAN"
            ),
            "linker_ticker_count": link_ticker_count,
            "phase3ar_scope": (
                "NEAR_MONEY_SNAPSHOT_TICKERS"
                if phase3ar_ticker_count is not None
                else "LATEST_CRYPTO_LINKS"
            ),
            "phase3ar_ticker_count": phase3ar_ticker_count,
        },
        "rate_limit": rate_limit,
        "summary": {
            "kalshi_api_status": (
                "RATE_LIMITED_KALSHI_API" if rate_limit_blocked else "COMPLETE"
            ),
            "data_complete": not rate_limit_blocked,
            "data_completeness": "partial" if rate_limit_blocked else "complete",
            "paper_ready_blocked_by_rate_limit": (
                rate_limit_blocked and int(raw_paper_ready_candidates or 0) > 0
            ),
            "crypto_series_refreshes": [
                _collect_summary_payload(
                    row,
                    series_ticker=(
                        crypto_series_tickers[index]
                        if index < len(crypto_series_tickers)
                        else None
                    ),
                    symbol=symbols[index] if index < len(symbols) else None,
                )
                for index, row in enumerate(collect_summaries)
            ],
            "collect_crypto_series_total_seconds": round(
                sum(row.collect_total_seconds for row in collect_summaries),
                3,
            ),
            "collect_crypto_series_market_pages_seconds": round(
                sum(row.market_pages_seconds for row in collect_summaries),
                3,
            ),
            "collect_crypto_series_orderbook_seconds": round(
                sum(row.orderbook_seconds for row in collect_summaries),
                3,
            ),
            "near_money_candidates": sum(
                row.near_money_candidates for row in collect_summaries
            ),
            "near_money_snapshots_inserted": sum(
                row.near_money_snapshots_inserted for row in collect_summaries
            ),
            "liquidity_first_scan_mode": (
                "RECENT_NONZERO_BOOK_DEPTH_PRIORITY"
                if near_money_only
                else "DISABLED_FULL_SCAN"
            ),
            "liquidity_hint_candidates": sum(
                row.liquidity_hint_candidates for row in collect_summaries
            ),
            "liquidity_first_snapshots_inserted": sum(
                row.liquidity_first_snapshots_inserted for row in collect_summaries
            ),
            "no_liquidity_hint_snapshots_inserted": sum(
                row.no_liquidity_hint_snapshots_inserted for row in collect_summaries
            ),
            "skipped_expired_windows": sum(
                row.skipped_expired_windows for row in collect_summaries
            ),
            "skipped_far_otm_rows": sum(row.skipped_far_otm_rows for row in collect_summaries),
            "per_symbol_snapshot_counts": dict(per_symbol_snapshot_counts),
            "per_symbol_liquidity_first_counts": dict(
                per_symbol_liquidity_first_counts
            ),
            "market_pages_processed": sum(
                row.market_pages_processed for row in collect_summaries
            ),
            "open_markets_seen": sum(row.markets_seen for row in collect_summaries),
            "open_market_snapshots_inserted": sum(
                row.snapshots_inserted for row in collect_summaries
            ),
            "crypto_price_rows_inserted": (
                ingest_summary.prices_inserted if ingest_summary is not None else 0
            ),
            "crypto_price_errors": ingest_summary.errors if ingest_summary is not None else [],
            "crypto_features_inserted": feature_summary.features_inserted,
            "crypto_links_created": link_summary.links_created,
            "crypto_links_already_linked": link_summary.already_linked,
            "crypto_exact_semantic_links": link_summary.exact_semantic_links,
            "snapshot_repairs_attempted": phase3ar_payload.get("repair_result", {}).get(
                "attempted",
                0,
            ),
            "snapshot_repairs_completed": phase3ar_payload.get("repair_result", {}).get(
                "repaired",
                0,
            ),
            "ready_to_forecast": phase3ar_summary.get("ready_to_forecast", 0),
            "forecast_scope": forecast_scope["scope"],
            "forecast_candidate_snapshots": forecast_scope["candidate_snapshots"],
            "forecast_current_window_snapshots": forecast_scope[
                "current_window_snapshots"
            ],
            "forecast_expired_window_snapshots_skipped": forecast_scope[
                "expired_window_snapshots_skipped"
            ],
            "forecast_unknown_window_snapshots": forecast_scope[
                "unknown_window_snapshots"
            ],
            "forecast_snapshots_scanned": forecast_summary.snapshots_scanned,
            "forecasts_inserted": forecast_summary.forecasts_inserted,
            "forecast_skipped": forecast_summary.skipped,
            "opportunity_markets_scanned": opportunity_summary.markets_scanned,
            "opportunity_scan_mode": getattr(opportunity_summary, "scan_mode", "UNKNOWN"),
            "opportunity_current_scope_count": getattr(
                opportunity_summary,
                "current_ticker_scope_count",
                None,
            ),
            "opportunity_historical_rows_excluded": getattr(
                opportunity_summary,
                "historical_rows_excluded",
                0,
            ),
            "opportunity_first_hard_blocker": getattr(
                opportunity_summary,
                "first_hard_blocker",
                None,
            ),
            "rankings_inserted": opportunity_summary.rankings_inserted,
            "opportunities_detected": opportunity_summary.opportunities_detected,
            "current_crypto_scope": current_crypto_scope.get("summary", {}),
            "phase3bc_pure_crypto_markets": phase3bc_summary.get("pure_crypto_markets", 0),
            "phase3bc_active_pure_crypto_markets": phase3bc_summary.get(
                "active_pure_crypto_markets",
                0,
            ),
            "phase3bc_mixed_or_cross_category_markets": phase3bc_summary.get(
                "mixed_or_cross_category_markets",
                0,
            ),
            "phase3bc_paper_ready_candidates": phase3bc_paper_ready_candidates,
            "phase3bc_raw_paper_ready_candidates": raw_paper_ready_candidates,
            "phase3bc_main_blocker": phase3bc_main_blocker,
        },
        "freshness": freshness,
        "stage_timings": stage_timings,
        "stage_duration_seconds": {
            row["stage"]: row["duration_seconds"] for row in stage_timings
        },
        "reports": {
            "phase3ar_json": str(phase3ar_artifacts.json_path),
            "phase3ar_markdown": str(phase3ar_artifacts.markdown_path),
            "opportunities_markdown": str(opportunities_path),
            "phase3bc_json": str(phase3bc_artifacts.json_path),
            "phase3bc_markdown": str(phase3bc_artifacts.markdown_path),
            "phase3bc_rows": str(phase3bc_artifacts.rows_path),
        },
        "recommended_next_action": _recommended_next_action(
            phase3bc_summary,
            freshness,
            rate_limit=rate_limit,
        ),
        "next_commands": [
            (
                "kalshi-bot phase3bc-r3-active-crypto-refresh --refresh-open-markets "
                "--near-money-only --market-max-pages 1 "
                f"--market-limit {DEFAULT_MARKET_PAGE_LIMIT} "
                "--crypto-market-scan-limit "
                f"{DEFAULT_CRYPTO_MARKET_SCAN_LIMIT} "
                f"--crypto-link-limit {DEFAULT_CRYPTO_LINK_SCAN_LIMIT} "
                f"--crypto-series-tickers {DEFAULT_CRYPTO_SERIES_TICKERS} "
                "--output-dir reports/phase3bc_r3"
            ),
            "kalshi-bot scheduler-plan --profile crypto-watch",
            (
                "kalshi-bot phase3bc-crypto-clean-opportunity-router "
                "--output-dir reports/phase3bc --limit 1000"
            ),
        ],
    }


def _freshness_summary(payload: dict[str, Any], *, cadence_minutes: int) -> dict[str, Any]:
    now = utc_now()
    rows = payload.get("rows", [])
    active_pure_rows = [
        row
        for row in rows
        if row.get("active_market") and row.get("structure_status") == "PURE_CRYPTO"
    ]
    return {
        "target_cadence_minutes": cadence_minutes,
        "active_pure_rows_checked": len(active_pure_rows),
        "fresh_active_pure_rows": sum(
            1 for row in active_pure_rows if _row_is_fresh(row, cadence_minutes, now=now)
        ),
        "stale_active_pure_rows": sum(
            1 for row in active_pure_rows if not _row_is_fresh(row, cadence_minutes, now=now)
        ),
        "latest_snapshot_at": _latest_timestamp(rows, "latest_snapshot_at"),
        "latest_forecast_at": _latest_timestamp(rows, "latest_forecast_at"),
        "latest_ranking_at": _latest_timestamp(rows, "latest_ranking_at"),
    }


def _row_is_fresh(row: dict[str, Any], cadence_minutes: int, *, now: Any) -> bool:
    for key in ("latest_snapshot_at", "latest_forecast_at", "latest_ranking_at"):
        value = parse_datetime(row.get(key))
        if value is None:
            return False
        age_minutes = (now - value).total_seconds() / 60
        if age_minutes > cadence_minutes:
            return False
    return True


def _latest_timestamp(rows: list[dict[str, Any]], key: str) -> str | None:
    values = [parse_datetime(row.get(key)) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values).isoformat()


def _forecast_snapshots_for_scope(
    snapshots: list[MarketSnapshot],
    *,
    current_windows_only: bool,
) -> tuple[list[MarketSnapshot], dict[str, Any]]:
    if not current_windows_only:
        return list(snapshots), {
            "scope": "ALL_ACTIVE_LINKED_CRYPTO",
            "candidate_snapshots": len(snapshots),
            "current_window_snapshots": len(snapshots),
            "expired_window_snapshots_skipped": 0,
            "unknown_window_snapshots": 0,
        }

    now = utc_now()
    current_rows: list[MarketSnapshot] = []
    expired = 0
    unknown = 0
    for snapshot in snapshots:
        close_time = crypto_ticker_close_time_utc(snapshot.ticker)
        if close_time is None:
            unknown += 1
            current_rows.append(snapshot)
            continue
        if close_time <= now:
            expired += 1
            continue
        current_rows.append(snapshot)
    return current_rows, {
        "scope": "CURRENT_ACTIVE_CRYPTO_WINDOWS",
        "candidate_snapshots": len(snapshots),
        "current_window_snapshots": len(current_rows),
        "expired_window_snapshots_skipped": expired,
        "unknown_window_snapshots": unknown,
    }


def _forecast_snapshot_candidates(
    session: Session,
    *,
    model_name: str,
    limit: int,
    near_money_only: bool,
    link_tickers: list[str] | None,
) -> tuple[list[MarketSnapshot], str]:
    effective_limit = max(limit, 0)
    tickers = list(dict.fromkeys(link_tickers or []))
    if near_money_only and tickers:
        return (
            latest_snapshots_for_forecasts(session, tickers)[:effective_limit],
            "COLLECTED_NEAR_MONEY_TICKERS",
        )
    return (
        latest_snapshots_for_model(session, model_name=model_name, limit=effective_limit)
        or [],
        "MODEL_LINKED_LATEST_SNAPSHOTS",
    )


def _collect_crypto_series(
    session: Session,
    *,
    series_tickers: list[str],
    symbols: list[str],
    limit: int,
    max_pages: int,
    near_money_only: bool,
    near_money_per_symbol_limit: int,
    near_money_window_limit: int,
    snapshot_fetch_concurrency: int,
) -> list[CollectOnceSummary]:
    with KalshiClient() as client:
        if near_money_only:
            latest_prices = _latest_prices_by_symbol(session, symbols)
            return [
                _collect_near_money_crypto_series(
                    session,
                    client=client,
                    series_ticker=series_ticker,
                    latest_prices=latest_prices,
                    limit=limit,
                    max_pages=max_pages,
                    per_symbol_limit=near_money_per_symbol_limit,
                    per_window_limit=near_money_window_limit,
                    snapshot_fetch_concurrency=snapshot_fetch_concurrency,
                )
                for series_ticker in series_tickers
            ]
        if not series_tickers:
            return [
                _collect_once_with_rate_limit_summary(
                    client,
                    session=session,
                    series_ticker=None,
                    limit=limit,
                    max_pages=max_pages,
                )
            ]
        return [
            _collect_once_with_rate_limit_summary(
                client,
                session=session,
                series_ticker=series_ticker,
                limit=limit,
                max_pages=max_pages,
            )
            for series_ticker in series_tickers
        ]


def _collect_once_with_rate_limit_summary(
    client: KalshiClient,
    *,
    session: Session,
    series_ticker: str | None,
    limit: int,
    max_pages: int,
) -> CollectOnceSummary:
    try:
        return collect_once(
            status="open",
            limit=limit,
            max_pages=max_pages,
            series_ticker=series_ticker,
            include_orderbook=True,
            generate_market_implied_forecasts=False,
            session=session,
            client=client,
        )
    except KalshiRetryError as exc:
        return _rate_limited_collect_summary(
            client,
            error=str(exc),
            stopped_reason=RATE_LIMITED_RETRY_EXHAUSTED,
        )


def _rate_limited_collect_summary(
    client: KalshiClient,
    *,
    error: str,
    stopped_reason: str,
    market_seconds: float = 0.0,
    rows_fetched_before_limit: int = 0,
) -> CollectOnceSummary:
    rate_limit_details = client.telemetry.as_dict(
        rows_fetched_before_limit=rows_fetched_before_limit
    )
    rate_limit_details["error"] = error
    rate_limit_status = str(rate_limit_details.get("status") or RATE_LIMITED_ABORTED)
    if rate_limit_status == "COMPLETE":
        rate_limit_status = RATE_LIMITED_ABORTED
        rate_limit_details["status"] = rate_limit_status
    return CollectOnceSummary(
        markets_seen=0,
        snapshots_inserted=0,
        forecasts_inserted=0,
        skipped_forecasts=0,
        db_location="shared-session",
        collection_status=rate_limit_status,
        stopped_reason=stopped_reason,
        market_pages_seconds=round(market_seconds, 3),
        rate_limit_status=rate_limit_status,
        rate_limited=True,
        rate_limit_details=rate_limit_details,
        data_complete=False,
    )


@dataclass(frozen=True)
class _LiquidityHint:
    ticker: str
    liquidity_score: Decimal
    spread: Decimal | None
    source: str
    observed_at: datetime | None


@dataclass(frozen=True)
class _NearMoneyCandidate:
    ticker: str
    symbol: str
    market: dict[str, Any]
    target_price: Decimal | None
    distance_ratio: Decimal
    close_time: datetime | None
    window_key: str
    liquidity_priority: int = 0
    liquidity_score_hint: Decimal | None = None
    spread_hint: Decimal | None = None
    liquidity_source: str | None = None


def _collect_near_money_crypto_series(
    session: Session,
    *,
    client: KalshiClient,
    series_ticker: str,
    latest_prices: dict[str, Decimal],
    limit: int,
    max_pages: int,
    per_symbol_limit: int,
    per_window_limit: int,
    snapshot_fetch_concurrency: int,
) -> CollectOnceSummary:
    started = time.monotonic()
    page_state: dict[str, Any] = {}
    market_started = time.monotonic()
    try:
        markets = list(
            client.iter_markets(
                status="open",
                limit=limit,
                max_pages=max_pages,
                series_ticker=series_ticker,
                page_callback=lambda payload: page_state.update(
                    _page_state_update(payload)
                ),
            )
        )
    except KalshiRetryError as exc:
        return _rate_limited_collect_summary(
            client,
            error=str(exc),
            stopped_reason=RATE_LIMITED_RETRY_EXHAUSTED,
            market_seconds=time.monotonic() - market_started,
        )
    market_seconds = time.monotonic() - market_started
    for market in markets:
        upsert_market(session, market)

    liquidity_hints = _recent_liquidity_hints(
        session,
        [str(market.get("ticker") or "") for market in markets],
    )
    selection = _select_near_money_candidates(
        markets,
        latest_prices=latest_prices,
        per_symbol_limit=per_symbol_limit,
        per_window_limit=per_window_limit,
        liquidity_hints=liquidity_hints,
    )
    candidates = selection["selected_candidates"]

    orderbook_started = time.monotonic()
    orderbooks = _fetch_orderbooks(
        client,
        candidates,
        concurrency=1 if client.telemetry.rate_limited else snapshot_fetch_concurrency,
    )
    orderbook_seconds = time.monotonic() - orderbook_started
    inserted_counts: Counter[str] = Counter()
    liquidity_inserted_counts: Counter[str] = Counter()
    for candidate in candidates:
        insert_market_snapshot(
            session=session,
            market_json=candidate.market,
            orderbook_json=orderbooks.get(candidate.ticker),
            captured_at=utc_now(),
        )
        inserted_counts[candidate.symbol] += 1
        if candidate.liquidity_priority > 0:
            liquidity_inserted_counts[candidate.symbol] += 1

    pages_processed = int(page_state.get("pages_processed") or 0)
    stopped_reason = page_state.get("stopped_reason")
    resume_cursor = page_state.get("resume_cursor")
    has_more = bool(page_state.get("has_more"))
    collection_status = (
        "PARTIAL_REFRESH_CONTINUABLE"
        if stopped_reason or has_more
        else "COMPLETE"
    )
    rate_limit_details = client.telemetry.as_dict(
        rows_fetched_before_limit=len(markets) + len(candidates)
    )
    rate_limit_status = str(rate_limit_details.get("status") or "COMPLETE")
    rate_limited = bool(rate_limit_details.get("rate_limited"))
    if rate_limited or rate_limit_status != "COMPLETE":
        collection_status = rate_limit_status
    return CollectOnceSummary(
        markets_seen=len(markets),
        snapshots_inserted=len(candidates),
        forecasts_inserted=0,
        skipped_forecasts=0,
        db_location="shared-session",
        collection_status=collection_status,
        stopped_reason=str(stopped_reason) if stopped_reason else None,
        resume_cursor=str(resume_cursor) if resume_cursor else None,
        market_pages_processed=pages_processed,
        snapshot_pages_processed=pages_processed,
        collect_total_seconds=round(time.monotonic() - started, 3),
        market_pages_seconds=round(market_seconds, 3),
        orderbook_seconds=round(orderbook_seconds, 3),
        near_money_candidates=int(selection["near_money_candidates"]),
        near_money_snapshots_inserted=len(candidates),
        liquidity_hint_candidates=int(selection["liquidity_hint_candidates"]),
        liquidity_first_snapshots_inserted=int(selection["liquidity_first_selected"]),
        no_liquidity_hint_snapshots_inserted=max(
            0,
            len(candidates) - int(selection["liquidity_first_selected"]),
        ),
        skipped_expired_windows=int(selection["skipped_expired_windows"]),
        skipped_far_otm_rows=int(selection["skipped_far_otm_rows"]),
        per_symbol_liquidity_first_counts=dict(liquidity_inserted_counts),
        per_symbol_snapshot_counts=dict(inserted_counts),
        snapshot_tickers=[candidate.ticker for candidate in candidates],
        rate_limit_status=rate_limit_status,
        rate_limited=rate_limited,
        rate_limit_details=rate_limit_details,
        data_complete=not rate_limited,
    )


def _select_near_money_candidates(
    markets: list[dict[str, Any]],
    *,
    latest_prices: dict[str, Decimal],
    per_symbol_limit: int,
    per_window_limit: int,
    liquidity_hints: dict[str, _LiquidityHint] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved_now = now or utc_now()
    hints = liquidity_hints or {}
    candidates: list[_NearMoneyCandidate] = []
    skipped_expired = 0
    for market in markets:
        ticker = str(market.get("ticker") or "")
        status = str(market.get("status") or "").lower()
        if not ticker or status not in {"open", "active"}:
            continue
        close_time = crypto_ticker_close_time_utc(ticker)
        if close_time is not None and close_time <= resolved_now:
            skipped_expired += 1
            continue
        symbol = _candidate_symbol(market)
        if symbol is None:
            continue
        target_price = _crypto_ticker_target_price(ticker)
        distance_ratio = _distance_ratio(target_price, latest_prices.get(symbol))
        liquidity_hint = hints.get(ticker)
        liquidity_score = (
            liquidity_hint.liquidity_score if liquidity_hint is not None else None
        )
        candidates.append(
            _NearMoneyCandidate(
                ticker=ticker,
                symbol=symbol,
                market=market,
                target_price=target_price,
                distance_ratio=distance_ratio,
                close_time=close_time,
                window_key=_crypto_window_key(ticker, close_time),
                liquidity_priority=1
                if liquidity_score is not None and liquidity_score > 0
                else 0,
                liquidity_score_hint=liquidity_score,
                spread_hint=liquidity_hint.spread if liquidity_hint is not None else None,
                liquidity_source=liquidity_hint.source
                if liquidity_hint is not None
                else None,
            )
        )

    selected: list[_NearMoneyCandidate] = []
    per_symbol_counts: Counter[str] = Counter()
    per_window_counts: Counter[tuple[str, str]] = Counter()
    for candidate in sorted(candidates, key=_near_money_sort_key):
        if per_symbol_counts[candidate.symbol] >= max(per_symbol_limit, 0):
            continue
        window_key = (candidate.symbol, candidate.window_key)
        if per_window_counts[window_key] >= max(per_window_limit, 0):
            continue
        selected.append(candidate)
        per_symbol_counts[candidate.symbol] += 1
        per_window_counts[window_key] += 1

    return {
        "selected_candidates": selected,
        "near_money_candidates": len(candidates),
        "liquidity_hint_candidates": sum(
            1 for row in candidates if row.liquidity_priority > 0
        ),
        "liquidity_first_selected": sum(
            1 for row in selected if row.liquidity_priority > 0
        ),
        "per_symbol_liquidity_first_selected_counts": dict(
            Counter(row.symbol for row in selected if row.liquidity_priority > 0)
        ),
        "skipped_expired_windows": skipped_expired,
        "skipped_far_otm_rows": max(0, len(candidates) - len(selected)),
        "per_symbol_candidate_counts": dict(Counter(row.symbol for row in candidates)),
        "per_symbol_selected_counts": dict(per_symbol_counts),
    }


def _recent_liquidity_hints(
    session: Session,
    tickers: list[str],
) -> dict[str, _LiquidityHint]:
    unique_tickers = [ticker for ticker in dict.fromkeys(tickers) if ticker]
    if not unique_tickers:
        return {}

    hints: dict[str, _LiquidityHint] = {}
    ranking_limit = max(len(unique_tickers) * 3, 1000)
    ranking_rows = session.scalars(
        select(MarketRanking)
        .where(
            MarketRanking.ticker.in_(unique_tickers),
            MarketRanking.forecast_model == MODEL_NAME,
        )
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(ranking_limit)
    )
    for row in ranking_rows:
        if row.ticker in hints:
            continue
        liquidity_score = to_decimal(row.liquidity_score)
        if liquidity_score is None or liquidity_score <= 0:
            continue
        hints[row.ticker] = _LiquidityHint(
            ticker=row.ticker,
            liquidity_score=liquidity_score,
            spread=to_decimal(row.spread),
            source="market_ranking",
            observed_at=row.ranked_at,
        )

    missing_tickers = [ticker for ticker in unique_tickers if ticker not in hints]
    if not missing_tickers:
        return hints

    snapshot_limit = max(len(missing_tickers) * 3, 1000)
    snapshot_rows = session.scalars(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker.in_(missing_tickers))
        .order_by(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id))
        .limit(snapshot_limit)
    )
    for row in snapshot_rows:
        if row.ticker in hints or not _snapshot_has_book(row):
            continue
        hints[row.ticker] = _LiquidityHint(
            ticker=row.ticker,
            liquidity_score=Decimal("1"),
            spread=to_decimal(row.spread),
            source="market_snapshot",
            observed_at=row.captured_at,
        )
    return hints


def _fetch_orderbooks(
    client: KalshiClient,
    candidates: list[_NearMoneyCandidate],
    *,
    concurrency: int,
) -> dict[str, dict[str, Any]]:
    if not candidates:
        return {}
    if concurrency <= 1:
        return {
            candidate.ticker: _safe_get_orderbook(client, candidate.ticker)
            for candidate in candidates
        }

    results: dict[str, dict[str, Any]] = {}
    max_workers = min(max(concurrency, 1), len(candidates))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_ticker = {
            executor.submit(_safe_get_orderbook, client, candidate.ticker): candidate.ticker
            for candidate in candidates
        }
        for future in as_completed(future_by_ticker):
            ticker = future_by_ticker[future]
            results[ticker] = future.result()
    return results


def _safe_get_orderbook(client: KalshiClient, ticker: str) -> dict[str, Any]:
    try:
        return client.get_orderbook(ticker)
    except KalshiClientError as exc:
        return {"error": str(exc), "ticker": ticker}


def _latest_prices_by_symbol(session: Session, symbols: list[str]) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for symbol in symbols:
        latest = get_latest_crypto_price(session, symbol)
        price = to_decimal(latest.price_usd if latest is not None else None)
        if price is not None:
            prices[symbol.upper()] = price
    return prices


def _candidate_symbol(market: dict[str, Any]) -> str | None:
    for value in (market.get("series_ticker"), market.get("event_ticker"), market.get("ticker")):
        symbol = symbol_from_event_ticker(value)
        if symbol is not None:
            return symbol
    return None


def _crypto_ticker_target_price(ticker: str) -> Decimal | None:
    match = CRYPTO_TARGET_PRICE_RE.search(str(ticker or "").upper())
    if match is None:
        return None
    return to_decimal(match.group("target"))


def _distance_ratio(target_price: Decimal | None, latest_price: Decimal | None) -> Decimal:
    if target_price is None or latest_price is None or latest_price <= 0:
        return Decimal("999999")
    return abs(target_price - latest_price) / latest_price


def _snapshot_has_book(snapshot: MarketSnapshot) -> bool:
    book_values = (
        snapshot.best_yes_bid,
        snapshot.best_yes_ask,
        snapshot.best_no_bid,
        snapshot.best_no_ask,
        snapshot.yes_bid_dollars,
        snapshot.yes_ask_dollars,
        snapshot.no_bid_dollars,
        snapshot.no_ask_dollars,
    )
    return any(to_decimal(value) is not None for value in book_values)


def _near_money_sort_key(candidate: _NearMoneyCandidate) -> tuple[Any, ...]:
    close_time = candidate.close_time or datetime.max.replace(tzinfo=UTC)
    liquidity_score = candidate.liquidity_score_hint or Decimal("0")
    spread = candidate.spread_hint if candidate.spread_hint is not None else Decimal("999999")
    return (
        -candidate.liquidity_priority,
        close_time,
        candidate.distance_ratio,
        -liquidity_score,
        spread,
        candidate.ticker,
    )


def _crypto_window_key(ticker: str, close_time: datetime | None) -> str:
    if close_time is not None:
        return close_time.isoformat()
    parts = str(ticker or "").split("-")
    return parts[1] if len(parts) > 1 else "UNKNOWN"


def _page_state_update(payload: dict[str, Any]) -> dict[str, Any]:
    event = payload.get("event")
    if event == "page":
        return {
            "pages_processed": int(payload.get("pages_seen") or 0),
            "resume_cursor": payload.get("resume_cursor"),
            "has_more": bool(payload.get("has_more")),
        }
    if event == "stop":
        return {
            "stopped_reason": payload.get("stop_reason"),
            "resume_cursor": payload.get("resume_cursor"),
        }
    return {}


def _aggregate_symbol_counts(rows: list[CollectOnceSummary]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(row.per_symbol_snapshot_counts)
    return counts


def _aggregate_liquidity_first_counts(rows: list[CollectOnceSummary]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(row.per_symbol_liquidity_first_counts)
    return counts


def _rate_limit_summary(rows: list[CollectOnceSummary]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "COMPLETE",
            "blocker": None,
            "rate_limited": False,
            "data_complete": True,
            "data_completeness": "complete",
            "affected_stages": [],
            "request_count": 0,
            "retry_count": 0,
            "rate_limited_count": 0,
            "retry_exhausted_count": 0,
            "total_sleep_seconds": 0.0,
            "rows_fetched_before_limit": 0,
            "top_endpoint": None,
            "endpoints": [],
        }

    endpoint_stats: dict[str, dict[str, Any]] = {}
    request_count = 0
    retry_count = 0
    rate_limited_count = 0
    retry_exhausted_count = 0
    total_sleep_seconds = 0.0
    rows_fetched_before_limit = 0
    statuses: set[str] = set()
    events: list[dict[str, Any]] = []

    for row in rows:
        details = row.rate_limit_details or {}
        status = str(row.rate_limit_status or details.get("status") or "COMPLETE")
        if row.rate_limited or status != "COMPLETE":
            statuses.add(status)
        request_count = max(request_count, int(details.get("request_count") or 0))
        retry_count = max(retry_count, int(details.get("retry_count") or 0))
        rate_limited_count = max(
            rate_limited_count,
            int(details.get("rate_limited_count") or 0),
        )
        retry_exhausted_count = max(
            retry_exhausted_count,
            int(details.get("retry_exhausted_count") or 0),
        )
        total_sleep_seconds = max(
            total_sleep_seconds,
            float(details.get("total_sleep_seconds") or 0.0),
        )
        row_fetched_before_limit = row.markets_seen + row.snapshots_inserted
        if row_fetched_before_limit <= 0:
            row_fetched_before_limit = int(details.get("rows_fetched_before_limit") or 0)
        rows_fetched_before_limit += row_fetched_before_limit
        for endpoint in details.get("endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            key = str(endpoint.get("endpoint") or "UNKNOWN")
            stat = endpoint_stats.setdefault(
                key,
                {
                    "endpoint": key,
                    "status_code": endpoint.get("status_code"),
                    "retry_count": 0,
                    "total_sleep_seconds": 0.0,
                    "retry_exhausted": False,
                },
            )
            stat["status_code"] = endpoint.get("status_code")
            stat["retry_count"] = max(
                int(stat["retry_count"]),
                int(endpoint.get("retry_count") or 0),
            )
            stat["total_sleep_seconds"] = max(
                float(stat["total_sleep_seconds"]),
                float(endpoint.get("total_sleep_seconds") or 0.0),
            )
            stat["retry_exhausted"] = bool(stat["retry_exhausted"]) or bool(
                endpoint.get("retry_exhausted")
            )
        events.extend(
            event for event in details.get("events") or [] if isinstance(event, dict)
        )

    rate_limited = bool(statuses) or rate_limited_count > 0 or retry_exhausted_count > 0
    if RATE_LIMITED_RETRY_EXHAUSTED in statuses or retry_exhausted_count > 0:
        status = RATE_LIMITED_RETRY_EXHAUSTED
    elif RATE_LIMITED_ABORTED in statuses:
        status = RATE_LIMITED_ABORTED
    elif rate_limited:
        status = RATE_LIMITED_PARTIAL
    else:
        status = "COMPLETE"
    endpoints = sorted(
        endpoint_stats.values(),
        key=lambda item: (-int(item["retry_count"]), str(item["endpoint"])),
    )
    top_endpoint = endpoints[0]["endpoint"] if endpoints else None
    return {
        "status": status,
        "blocker": "RATE_LIMITED_KALSHI_API" if rate_limited else None,
        "rate_limited": rate_limited,
        "data_complete": not rate_limited,
        "data_completeness": "partial" if rate_limited else "complete",
        "affected_stages": ["collect_crypto_series"] if rate_limited else [],
        "request_count": request_count,
        "retry_count": retry_count,
        "rate_limited_count": rate_limited_count,
        "retry_exhausted_count": retry_exhausted_count,
        "total_sleep_seconds": round(total_sleep_seconds, 3),
        "rows_fetched_before_limit": rows_fetched_before_limit,
        "top_endpoint": top_endpoint,
        "endpoints": endpoints,
        "events": events[-50:],
    }


def _collected_snapshot_tickers(rows: list[CollectOnceSummary]) -> list[str]:
    tickers: list[str] = []
    for row in rows:
        tickers.extend(row.snapshot_tickers)
    return list(dict.fromkeys(tickers))


def _parse_csv(value: str) -> list[str]:
    return [part.strip().upper() for part in value.split(",") if part.strip()]


def _collect_summary_payload(
    summary: CollectOnceSummary,
    *,
    series_ticker: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    return {
        "series_ticker": series_ticker,
        "symbol": symbol,
        "markets_seen": summary.markets_seen,
        "snapshots_inserted": summary.snapshots_inserted,
        "market_pages_processed": summary.market_pages_processed,
        "snapshot_pages_processed": summary.snapshot_pages_processed,
        "collection_status": summary.collection_status,
        "rate_limit_status": summary.rate_limit_status,
        "rate_limited": summary.rate_limited,
        "data_complete": summary.data_complete,
        "rate_limit_details": summary.rate_limit_details,
        "stopped_reason": summary.stopped_reason,
        "resume_cursor": summary.resume_cursor,
        "collect_total_seconds": summary.collect_total_seconds,
        "market_pages_seconds": summary.market_pages_seconds,
        "orderbook_seconds": summary.orderbook_seconds,
        "near_money_candidates": summary.near_money_candidates,
        "near_money_snapshots_inserted": summary.near_money_snapshots_inserted,
        "liquidity_hint_candidates": summary.liquidity_hint_candidates,
        "liquidity_first_snapshots_inserted": summary.liquidity_first_snapshots_inserted,
        "no_liquidity_hint_snapshots_inserted": (
            summary.no_liquidity_hint_snapshots_inserted
        ),
        "skipped_expired_windows": summary.skipped_expired_windows,
        "skipped_far_otm_rows": summary.skipped_far_otm_rows,
        "per_symbol_liquidity_first_counts": dict(
            summary.per_symbol_liquidity_first_counts
        ),
        "per_symbol_snapshot_counts": dict(summary.per_symbol_snapshot_counts),
        "snapshot_ticker_count": len(summary.snapshot_tickers),
    }


def _recommended_next_action(
    summary: dict[str, Any],
    freshness: dict[str, Any],
    *,
    rate_limit: dict[str, Any] | None = None,
) -> str:
    if rate_limit and rate_limit.get("rate_limited"):
        endpoint = rate_limit.get("top_endpoint") or "Kalshi public API"
        status = rate_limit.get("status") or "RATE_LIMITED_PARTIAL"
        return (
            f"Kalshi API refresh is {status} at {endpoint}; wait for the backoff window "
            "or rerun a bounded refresh later. Keep paper-ready blocked until catalog "
            "and book data are complete."
        )
    if summary.get("paper_ready_candidates", 0) > 0:
        return (
            "Review paper-ready crypto rows manually; keep execution disabled until risk "
            "and human approval gates pass."
        )
    if summary.get("active_pure_crypto_markets", 0) == 0:
        return (
            "Continue 15-minute crypto refreshes with bounded open-market snapshots until "
            "active pure crypto markets appear."
        )
    if freshness.get("stale_active_pure_rows", 0) > 0:
        return (
            "Refresh crypto snapshots, forecasts, and rankings again; active pure rows are "
            "older than the 15-minute cadence."
        )
    return "No paper-ready rows yet; wait for price movement, liquidity, spread, or EV to improve."


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    freshness = payload["freshness"]
    lines = [
        "# Phase 3BC-R3 Active Pure Crypto Refresh",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "- PAPER ONLY: no live/demo execution or order writes.",
        f"- Target cadence: `{payload['cadence']['target_minutes']} minutes`",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: `{value}`")
    rate_limit = payload.get("rate_limit", {})
    lines.extend(["", "## Kalshi API Rate Limit", ""])
    for key in (
        "status",
        "blocker",
        "data_completeness",
        "retry_count",
        "total_sleep_seconds",
        "rows_fetched_before_limit",
        "top_endpoint",
    ):
        lines.append(f"- {key}: `{rate_limit.get(key)}`")
    endpoints = rate_limit.get("endpoints") or []
    if endpoints:
        lines.append("- endpoints:")
        for endpoint in endpoints[:10]:
            lines.append(
                "  - "
                f"`{endpoint.get('endpoint')}` status `{endpoint.get('status_code')}` "
                f"retries `{endpoint.get('retry_count')}` slept "
                f"`{endpoint.get('total_sleep_seconds')}`s"
            )
    lines.extend(["", "## Freshness", ""])
    for key, value in freshness.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Stage Timings", ""])
    if payload.get("stage_timings"):
        for row in payload["stage_timings"]:
            lines.append(f"- {row['stage']}: `{row['duration_seconds']}s`")
    else:
        lines.append("- none: `0`")
    lines.extend(
        [
            "",
            "## Reports",
            "",
        ]
    )
    for key, value in payload["reports"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
            "## Next Commands",
            "",
            "```bash",
            *payload["next_commands"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _recent_complete_refresh_exists(
    output_dir: Path,
    *,
    cadence_minutes: int,
    now: datetime | None = None,
) -> bool:
    path = output_dir / "phase3bc_r3_active_crypto_refresh.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else {}
    if isinstance(rate_limit, dict) and rate_limit.get("rate_limited"):
        return False
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    if isinstance(summary, dict) and summary.get("data_complete") is False:
        return False
    generated_at = parse_datetime(payload.get("generated_at"))
    if generated_at is None:
        return False
    max_age_seconds = min(120, max(30, int(cadence_minutes) * 30))
    return ((now or utc_now()) - generated_at).total_seconds() < max_age_seconds


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
