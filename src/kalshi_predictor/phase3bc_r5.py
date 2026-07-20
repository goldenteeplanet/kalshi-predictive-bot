from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    MarketRanking,
)
from kalshi_predictor.forecasting.registry import (
    latest_snapshots_for_forecasts,
    run_forecast_models,
)
from kalshi_predictor.learning.config import learning_paper_settings
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.phase3ar import repair_crypto_snapshots_for_tickers
from kalshi_predictor.phase3bc_r3 import (
    DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    DEFAULT_CRYPTO_SERIES_TICKERS,
    DEFAULT_MARKET_PAGE_LIMIT,
    DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    write_phase3bc_r3_active_crypto_refresh_report,
)
from kalshi_predictor.phase3bc_r4 import write_phase3bc_r4_crypto_ev_risk_diagnostics_report
from kalshi_predictor.phase3bc_r7 import (
    write_phase3bc_r7_crypto_ranking_coverage_repair_report,
)
from kalshi_predictor.position_sizing.service import ensure_paper_decision_sized
from kalshi_predictor.runtime_stage_heartbeat import AtomicStageHeartbeat
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BC_R5_VERSION = "phase3bc_r5_crypto_freshness_watch_positive_ev_trigger"
MODEL_NAME = "crypto_v2"
SNAPSHOT_REFRESH_CANDIDATE_FILTER = (
    "ACTIVE_OPEN_PURE_CRYPTO_EV_NEAR_MISS_OR_STALE_MAINTENANCE"
)
PREFLIGHT_LOW_SCORE = "LOW_SCORE"
PREFLIGHT_LOW_EDGE = "LOW_EDGE"
PREFLIGHT_LIQUIDITY_ZERO = "LIQUIDITY_ZERO"
PREFLIGHT_SNAPSHOT_STALE = "SNAPSHOT_STALE"
PREFLIGHT_RANKING_GAP = "RANKING_GAP"
PREFLIGHT_RISK_MISSING = "RISK_MISSING"
SNAPSHOT_REFRESH_ISSUES = {"SNAPSHOT_STALE", "SNAPSHOT_MISSING"}
EXACT_TICKER_NOT_REFRESHED = "EXACT_TICKER_NOT_REFRESHED"
FORECAST_REFRESH_PENDING_AFTER_SNAPSHOT_REFRESH = (
    "FORECAST_REFRESH_PENDING_AFTER_SNAPSHOT_REFRESH"
)
FORECAST_REFRESHED_STILL_STALE = "FORECAST_REFRESHED_STILL_STALE"
FRESHNESS_COMPLETE = "COMPLETE"
EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
LOW_EDGE_OR_SCORE_BLOCK = "LOW_EDGE_OR_SCORE_BLOCK"
POSITIVE_EV_NO_EXECUTABLE_BOOK = "POSITIVE_EV_NO_EXECUTABLE_BOOK"
RISK_OR_SIZE_BLOCK = "RISK_OR_SIZE_BLOCK"
PREFLIGHT_BLOCKER_ORDER = (
    PREFLIGHT_LOW_SCORE,
    PREFLIGHT_LOW_EDGE,
    PREFLIGHT_LIQUIDITY_ZERO,
    PREFLIGHT_SNAPSHOT_STALE,
    PREFLIGHT_RANKING_GAP,
    PREFLIGHT_RISK_MISSING,
)
EV_NEAR_MISS_BAND = Decimal("0.01")
EV_NEAR_MISS_BAND_CENTS = "1.0"


@dataclass(frozen=True)
class Phase3BCR5ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    preflight_rows_path: Path
    history_path: Path
    phase3bc_r3_json_path: Path
    phase3bc_r7_json_path: Path
    phase3bc_r4_json_path: Path


def write_phase3bc_r5_crypto_freshness_watch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Path = Path("reports/phase3bc_r7"),
    settings: Settings | None = None,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: str = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: str = "coinbase",
    refresh_open_markets: bool = True,
    external_crypto_ingest: bool = True,
    repair_snapshots: bool = False,
    forecast_current_windows_only: bool = True,
    generate_opportunity_report: bool = False,
    market_limit: int = DEFAULT_MARKET_PAGE_LIMIT,
    market_max_pages: int = 1,
    crypto_market_scan_limit: int = DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    crypto_link_limit: int = DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    forecast_limit: int = 1000,
    opportunity_limit: int = 500,
    phase3bc_limit: int = 1000,
    cadence_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    freshness_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    max_preflight: int = 10,
    risk_preflight: bool = True,
    ranking_repair: bool = True,
    ranking_repair_limit: int = 500,
    exact_snapshot_refresh: bool = True,
    exact_snapshot_refresh_limit: int = 50,
    near_money_only: bool = True,
    near_money_per_symbol_limit: int = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: int = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: int = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    cycle_number: int = 1,
    total_cycles: int = 1,
) -> Phase3BCR5ArtifactSet:
    """Run the crypto refresh/diagnostic chain and only risk-preflight clean rows."""
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3bc_r5_crypto_freshness_watch.json"
    markdown_path = output_dir / "phase3bc_r5_crypto_freshness_watch.md"
    preflight_rows_path = output_dir / "phase3bc_r5_positive_ev_preflight_rows.json"
    history_path = output_dir / "phase3bc_r5_crypto_freshness_watch_history.jsonl"
    previous_payload = _read_json(json_path)
    stage_timer = _StageTimer(
        output_dir,
        cycle_number=cycle_number,
        total_cycles=total_cycles,
    )

    stage_timer.mark("phase3bc_r3_refresh")
    r3_artifacts = write_phase3bc_r3_active_crypto_refresh_report(
        session,
        output_dir=phase3bc_r3_output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        settings=resolved,
        symbols=symbols,
        crypto_series_tickers=crypto_series_tickers,
        source=source,
        refresh_open_markets=refresh_open_markets,
        external_crypto_ingest=external_crypto_ingest,
        repair_snapshots=repair_snapshots,
        forecast_current_windows_only=forecast_current_windows_only,
        generate_opportunity_report=generate_opportunity_report,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        crypto_market_scan_limit=crypto_market_scan_limit,
        crypto_link_limit=crypto_link_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
        phase3bc_limit=phase3bc_limit,
        cadence_minutes=cadence_minutes,
        near_money_only=near_money_only,
        near_money_per_symbol_limit=near_money_per_symbol_limit,
        near_money_window_limit=near_money_window_limit,
        snapshot_fetch_concurrency=snapshot_fetch_concurrency,
    )
    stage_timer.mark("phase3bc_r7_ranking_repair")
    r7_artifacts = write_phase3bc_r7_crypto_ranking_coverage_repair_report(
        session,
        output_dir=phase3bc_r7_output_dir,
        settings=resolved,
        limit=phase3bc_limit,
        freshness_minutes=freshness_minutes,
        repair_rankings=ranking_repair,
        repair_limit=ranking_repair_limit,
    )
    stage_timer.mark("phase3bc_r4_diagnostics")
    r4_artifacts = write_phase3bc_r4_crypto_ev_risk_diagnostics_report(
        session,
        output_dir=phase3bc_r4_output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        settings=resolved,
        limit=phase3bc_limit,
        freshness_minutes=freshness_minutes,
    )

    r3_payload = _read_json(r3_artifacts.json_path)
    r7_payload = _read_json(r7_artifacts.json_path)
    r4_payload = _read_json(r4_artifacts.json_path)

    stage_timer.mark("exact_snapshot_refresh")
    exact_snapshot_refresh_result = _maybe_refresh_exact_snapshots(
        session,
        r4_payload,
        enabled=exact_snapshot_refresh,
        limit=exact_snapshot_refresh_limit,
    )
    stage_timer.mark("exact_forecast_refresh")
    exact_forecast_refresh_result = _maybe_refresh_exact_forecasts(
        session,
        exact_snapshot_refresh_result,
        limit=forecast_limit,
    )
    if int(exact_snapshot_refresh_result.get("repaired") or 0) > 0:
        stage_timer.mark("phase3bc_r7_after_snapshot_refresh")
        r7_artifacts = write_phase3bc_r7_crypto_ranking_coverage_repair_report(
            session,
            output_dir=phase3bc_r7_output_dir,
            settings=resolved,
            limit=phase3bc_limit,
            freshness_minutes=freshness_minutes,
            repair_rankings=ranking_repair,
            repair_limit=ranking_repair_limit,
        )
        stage_timer.mark("phase3bc_r4_after_snapshot_refresh")
        r4_artifacts = write_phase3bc_r4_crypto_ev_risk_diagnostics_report(
            session,
            output_dir=phase3bc_r4_output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            settings=resolved,
            limit=phase3bc_limit,
            freshness_minutes=freshness_minutes,
        )
        r7_payload = _read_json(r7_artifacts.json_path)
        r4_payload = _read_json(r4_artifacts.json_path)

    stage_timer.mark("select_preflight_candidates")
    phase3bc_payload = _read_json(r4_artifacts.phase3bc_json_path)
    rows = list(phase3bc_payload.get("rows", []))
    risk_by_ticker = _latest_risk_decisions_by_ticker(
        session,
        [str(row.get("ticker")) for row in rows if row.get("ticker")],
    )
    candidates, blocked = select_phase3bc_r5_preflight_candidates(
        rows,
        risk_by_ticker=risk_by_ticker,
        freshness_minutes=freshness_minutes,
        max_preflight=max_preflight,
    )
    stage_timer.mark("phase3m_phase3n_preflight")
    preflight_results = (
        _run_risk_preflight(
            session,
            candidates,
            settings=_preflight_settings(resolved),
        )
        if risk_preflight
        else []
    )
    stage_timer.mark("build_report_payload")
    payload = build_phase3bc_r5_payload(
        r3_payload=r3_payload,
        r7_payload=r7_payload,
        r4_payload=r4_payload,
        phase3bc_payload=phase3bc_payload,
        candidates=candidates,
        blocked=blocked,
        preflight_results=preflight_results,
        risk_preflight=risk_preflight,
        exact_snapshot_refresh_result=exact_snapshot_refresh_result,
        exact_forecast_refresh_result=exact_forecast_refresh_result,
        stage_timings=stage_timer.timings,
        options={
            "symbols": symbols,
            "crypto_series_tickers": crypto_series_tickers,
            "source": source,
            "refresh_open_markets": refresh_open_markets,
            "external_crypto_ingest": external_crypto_ingest,
            "repair_snapshots": repair_snapshots,
            "forecast_current_windows_only": forecast_current_windows_only,
            "generate_opportunity_report": generate_opportunity_report,
            "market_limit": market_limit,
            "market_max_pages": market_max_pages,
            "crypto_market_scan_limit": crypto_market_scan_limit,
            "crypto_link_limit": crypto_link_limit,
            "forecast_limit": forecast_limit,
            "opportunity_limit": opportunity_limit,
            "phase3bc_limit": phase3bc_limit,
            "cadence_minutes": cadence_minutes,
            "freshness_minutes": freshness_minutes,
            "max_preflight": max_preflight,
            "ranking_repair": ranking_repair,
            "ranking_repair_limit": ranking_repair_limit,
            "exact_snapshot_refresh": exact_snapshot_refresh,
            "exact_snapshot_refresh_limit": exact_snapshot_refresh_limit,
            "near_money_only": near_money_only,
            "near_money_per_symbol_limit": near_money_per_symbol_limit,
            "near_money_window_limit": near_money_window_limit,
            "snapshot_fetch_concurrency": snapshot_fetch_concurrency,
        },
        reports={
            "phase3bc_r3_json": str(r3_artifacts.json_path),
            "phase3bc_r3_markdown": str(r3_artifacts.markdown_path),
            "phase3bc_r7_json": str(r7_artifacts.json_path),
            "phase3bc_r7_markdown": str(r7_artifacts.markdown_path),
            "phase3bc_r4_json": str(r4_artifacts.json_path),
            "phase3bc_r4_markdown": str(r4_artifacts.markdown_path),
            "phase3bc_json": str(r4_artifacts.phase3bc_json_path),
            "phase3bc_rows": str(r4_artifacts.phase3bc_rows_path),
        },
        cycle_number=cycle_number,
        total_cycles=total_cycles,
        previous_payload=previous_payload,
    )

    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    dashboard_truth_refresh = _write_post_refresh_dashboard_truth(
        session,
        output_dir=output_dir,
        settings=resolved,
    )
    payload.setdefault("summary", {})["post_refresh_dashboard_truth_status"] = (
        dashboard_truth_refresh["status"]
    )
    payload.setdefault("reports", {})["post_refresh_dashboard_truth"] = (
        dashboard_truth_refresh
    )
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    preflight_rows_path.write_text(
        json.dumps(preflight_results, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_history_row(payload), sort_keys=True, default=str) + "\n")
    stage_timer.mark("complete")
    return Phase3BCR5ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        preflight_rows_path=preflight_rows_path,
        history_path=history_path,
        phase3bc_r3_json_path=r3_artifacts.json_path,
        phase3bc_r7_json_path=r7_artifacts.json_path,
        phase3bc_r4_json_path=r4_artifacts.json_path,
    )


def _write_post_refresh_dashboard_truth(
    session: Session,
    *,
    output_dir: Path,
    settings: Settings,
) -> dict[str, Any]:
    reports_dir = _standard_reports_dir(output_dir)
    if reports_dir is None:
        return {
            "status": "SKIPPED_NON_STANDARD_OUTPUT_DIR",
            "reason": "Phase 3AW dashboard truth reads the standard reports/phase3bc_r5 path.",
        }
    try:
        from kalshi_predictor.phase3aw import write_phase3aw_dashboard_truth_report
        from kalshi_predictor.phase3bc_r6 import write_phase3bc_r5_status_report

        status_artifacts = write_phase3bc_r5_status_report(
            output_dir=reports_dir / "phase3bc_r5",
        )
        truth_artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=reports_dir / "phase3aw",
            reports_dir=reports_dir,
            settings=settings,
            command_args=[
                "phase3bc-r5-crypto-freshness-watch",
                "post-refresh-dashboard-truth",
            ],
        )
    except Exception as exc:  # pragma: no cover - defensive report hook
        return {
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "reports_dir": str(reports_dir),
        }
    return {
        "status": "REFRESHED",
        "reports_dir": str(reports_dir),
        "r5_status_json": str(status_artifacts.json_path),
        "dashboard_truth_json": str(truth_artifacts.dashboard_truth_path),
        "dashboard_truth_summary": str(truth_artifacts.executive_summary_path),
    }


def _standard_reports_dir(output_dir: Path) -> Path | None:
    if output_dir.name != "phase3bc_r5":
        return None
    reports_dir = output_dir.parent
    if not reports_dir.name:
        return None
    return reports_dir


def select_phase3bc_r5_preflight_candidates(
    rows: list[dict[str, Any]],
    *,
    risk_by_ticker: dict[str, dict[str, Any]],
    freshness_minutes: int,
    max_preflight: int = 10,
    now: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return rows that are clean enough for paper-only Phase 3M/3N preflight."""
    resolved_now = now or utc_now()
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in rows:
        reason = _candidate_block_reason(
            row,
            risk=risk_by_ticker.get(str(row.get("ticker"))),
            freshness_minutes=freshness_minutes,
            now=resolved_now,
        )
        risk = risk_by_ticker.get(str(row.get("ticker")))
        payload = _candidate_payload(row, risk=risk)
        payload["preflight_blockers"] = _candidate_preflight_blockers(
            row,
            risk=risk,
            freshness_minutes=freshness_minutes,
            now=resolved_now,
        )
        if reason is None:
            candidates.append(payload)
        elif row.get("active_market") and row.get("structure_status") == "PURE_CRYPTO":
            payload["blocked_reason"] = reason
            blocked.append(payload)
    candidates.sort(key=_candidate_sort_key, reverse=True)
    blocked.sort(key=_candidate_sort_key, reverse=True)
    return candidates[: max(0, max_preflight)], blocked


def build_phase3bc_r5_payload(
    *,
    r3_payload: dict[str, Any],
    r7_payload: dict[str, Any],
    r4_payload: dict[str, Any],
    phase3bc_payload: dict[str, Any],
    candidates: list[dict[str, Any]],
    blocked: list[dict[str, Any]],
    preflight_results: list[dict[str, Any]],
    risk_preflight: bool,
    options: dict[str, Any],
    reports: dict[str, Any],
    exact_snapshot_refresh_result: dict[str, Any] | None = None,
    exact_forecast_refresh_result: dict[str, Any] | None = None,
    stage_timings: list[dict[str, Any]] | None = None,
    cycle_number: int = 1,
    total_cycles: int = 1,
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = utc_now()
    r4_summary = r4_payload.get("summary", {})
    r7_summary = r7_payload.get("summary", {})
    phase3bc_summary = phase3bc_payload.get("summary", {})
    true_ranking_gap_after_repair = _true_ranking_gap_after_repair(
        r4_summary=r4_summary,
        r7_summary=r7_summary,
    )
    preflight_actions = Counter(
        str(row.get("phase3n_action") or "UNKNOWN") for row in preflight_results
    )
    blocked_reasons = Counter(str(row.get("blocked_reason") or "UNKNOWN") for row in blocked)
    positive_ev_blocked = [row for row in blocked if _positive_expected_value(row)]
    positive_ev_actionability_rows_all = [*candidates, *positive_ev_blocked]
    positive_ev_expired_window = [
        row
        for row in positive_ev_actionability_rows_all
        if not _current_crypto_window(row, now=generated_at)
    ]
    positive_ev_rows_for_actionability = [
        row
        for row in positive_ev_actionability_rows_all
        if _current_crypto_window(row, now=generated_at)
    ]
    positive_ev_no_executable_book = [
        row for row in positive_ev_rows_for_actionability if _no_executable_book(row)
    ]
    positive_ev_liquidity_positive = [
        row for row in positive_ev_rows_for_actionability if _liquidity_positive(row)
    ]
    positive_ev_clean_book = [
        row for row in positive_ev_rows_for_actionability if _clean_executable_book(row)
    ]
    positive_ev_snapshot_stale = [
        row
        for row in positive_ev_rows_for_actionability
        if _row_has_snapshot_stale_blocker(row)
    ]
    positive_ev_forecast_stale = [
        row
        for row in positive_ev_rows_for_actionability
        if _row_has_forecast_stale_blocker(row)
    ]
    positive_ev_spread_blocked = [
        row
        for row in positive_ev_rows_for_actionability
        if _row_has_spread_blocker(row)
    ]
    positive_ev_clean_book_risk_missing = [
        row for row in positive_ev_clean_book if _row_has_missing_risk(row)
    ]
    ev_calibration = _ev_calibration(
        r4_payload,
        near_miss_band=EV_NEAR_MISS_BAND,
        now=generated_at,
    )
    liquidity_watch_rows = _liquidity_watch_rows(
        positive_ev_rows_for_actionability,
        ev_calibration,
    )
    liquidity_emergence = _liquidity_emergence(
        previous_payload or {},
        liquidity_watch_rows,
    )
    preflight_blockers = Counter(
        blocker
        for row in positive_ev_blocked
        for blocker in row.get("preflight_blockers", [])
    )
    preflight_blocker_counts = {
        blocker: preflight_blockers.get(blocker, 0)
        for blocker in PREFLIGHT_BLOCKER_ORDER
    }
    snapshot_refresh_result = exact_snapshot_refresh_result or {}
    forecast_refresh_result = exact_forecast_refresh_result or {}
    stage_duration_seconds = _stage_duration_seconds(stage_timings or [])
    slowest_stage = _slowest_stage(stage_duration_seconds)
    summary = {
        "cycle_number": cycle_number,
        "total_cycles": total_cycles,
        "active_pure_crypto_rows": r4_summary.get("active_pure_crypto_rows", 0),
        "current_active_window_rows": r4_summary.get("current_active_window_rows", 0),
        "expired_crypto_window_rows": r4_summary.get("expired_crypto_window_rows", 0),
        "paper_ready_candidates": r4_summary.get("paper_ready_candidates", 0),
        "positive_ev_preflight_candidates": len(candidates),
        "phase3m_phase3n_preflight_attempted": len(preflight_results),
        "risk_preflight_enabled": risk_preflight,
        "risk_preflight_skipped": max(0, len(candidates) - len(preflight_results)),
        "no_positive_ev_rows": r4_summary.get("no_positive_ev_rows", 0),
        "missing_or_stale_ranking_rows": r4_summary.get("missing_or_stale_ranking_rows", 0),
        "true_ranking_gap_after_repair": true_ranking_gap_after_repair,
        "snapshot_stale_rows": r4_summary.get("snapshot_stale_rows", 0),
        "forecast_stale_rows": r4_summary.get("forecast_stale_rows", 0),
        "snapshot_missing_rows": r4_summary.get("snapshot_missing_rows", 0),
        "forecast_missing_rows": r4_summary.get("forecast_missing_rows", 0),
        "ranking_missing_rows": r4_summary.get("ranking_missing_rows", 0),
        "ranking_stale_rows": r4_summary.get("ranking_stale_rows", 0),
        "ranking_before_forecast_rows": r4_summary.get("ranking_before_forecast_rows", 0),
        "positive_ev_rows": r4_summary.get("positive_ev_rows", 0),
        "positive_ev_current_actionability_rows": len(positive_ev_rows_for_actionability),
        "positive_ev_expired_window_rows": len(positive_ev_expired_window),
        "positive_ev_blocked_preflight_rows": len(positive_ev_blocked),
        "positive_ev_no_executable_book_rows": len(positive_ev_no_executable_book),
        "positive_ev_liquidity_positive_rows": len(positive_ev_liquidity_positive),
        "positive_ev_clean_book_rows": len(positive_ev_clean_book),
        "positive_ev_snapshot_stale_rows": len(positive_ev_snapshot_stale),
        "positive_ev_forecast_stale_rows": len(positive_ev_forecast_stale),
        "positive_ev_spread_blocked_rows": len(positive_ev_spread_blocked),
        "positive_ev_clean_book_risk_missing_rows": len(
            positive_ev_clean_book_risk_missing
        ),
        "preflight_blocker_counts": preflight_blocker_counts,
        "ev_calibration_state": ev_calibration["state"],
        "ev_gate_cents": "0.0",
        "ev_near_miss_band_cents": EV_NEAR_MISS_BAND_CENTS,
        "best_current_expected_value": ev_calibration["best_expected_value"],
        "best_current_expected_value_cents": ev_calibration["best_expected_value_cents"],
        "best_ev_candidate_ticker": ev_calibration["best_candidate_ticker"],
        "best_ev_gap_to_positive_cents": ev_calibration["best_gap_to_positive_cents"],
        "ev_near_miss_rows": ev_calibration["near_miss_rows"],
        "ev_near_miss_liquidity_positive_rows": ev_calibration[
            "near_miss_liquidity_positive_rows"
        ],
        "ev_near_miss_clean_execution_rows": ev_calibration[
            "near_miss_clean_execution_rows"
        ],
        "liquidity_emergence_rows": liquidity_emergence["liquidity_emergence_rows"],
        "positive_ev_liquidity_emergence_rows": liquidity_emergence[
            "positive_ev_liquidity_emergence_rows"
        ],
        "near_miss_liquidity_emergence_rows": liquidity_emergence[
            "near_miss_liquidity_emergence_rows"
        ],
        "clean_execution_emergence_rows": liquidity_emergence[
            "clean_execution_emergence_rows"
        ],
        "positive_ev_clean_execution_emergence_rows": liquidity_emergence[
            "positive_ev_clean_execution_emergence_rows"
        ],
        "near_miss_clean_book_emergence_rows": liquidity_emergence[
            "near_miss_clean_book_emergence_rows"
        ],
        "liquidity_emergence_top_tickers": liquidity_emergence[
            "liquidity_emergence_top_tickers"
        ],
        "missing_executable_price_rows": ev_calibration["missing_executable_price_rows"],
        "clean_execution_rows": r4_summary.get("clean_execution_rows", 0),
        "risk_ready_rows": r4_summary.get("risk_ready_rows", 0),
        "spread_or_liquidity_blocked_rows": r4_summary.get(
            "spread_or_liquidity_blocked_rows",
            0,
        ),
        "exact_snapshot_refresh_attempted": (
            snapshot_refresh_result
        ).get("attempted", 0),
        "exact_snapshot_refresh_repaired": (
            snapshot_refresh_result
        ).get("repaired", 0),
        "exact_snapshot_refresh_selected": len(
            snapshot_refresh_result.get("selected_tickers") or []
        ),
        "exact_snapshot_refresh_candidate_filter": snapshot_refresh_result.get(
            "candidate_filter",
            SNAPSHOT_REFRESH_CANDIDATE_FILTER,
        ),
        "exact_snapshot_refresh_active_open_candidates": snapshot_refresh_result.get(
            "active_open_candidates",
            0,
        ),
        "exact_snapshot_refresh_book_visible_candidates": snapshot_refresh_result.get(
            "book_visible_candidates",
            0,
        ),
        "exact_snapshot_refresh_no_book_recheck_candidates": (
            snapshot_refresh_result.get("no_book_recheck_candidates", 0)
        ),
        "exact_snapshot_refresh_clean_execution_candidates": snapshot_refresh_result.get(
            "clean_execution_candidates",
            0,
        ),
        "exact_snapshot_refresh_stale_current_window_maintenance_candidates": (
            snapshot_refresh_result.get("stale_current_window_maintenance_candidates", 0)
        ),
        "exact_snapshot_refresh_positive_ev_candidates": snapshot_refresh_result.get(
            "positive_ev_candidates",
            0,
        ),
        "exact_snapshot_refresh_near_miss_candidates": snapshot_refresh_result.get(
            "near_miss_candidates",
            0,
        ),
        "exact_snapshot_refresh_unselected_active_open_candidates": (
            snapshot_refresh_result.get("unselected_active_open_candidates", 0)
        ),
        "exact_snapshot_refresh_unselected_reason": snapshot_refresh_result.get(
            "unselected_reason"
        ),
        "exact_snapshot_refresh_unselected_tickers": snapshot_refresh_result.get(
            "unselected_tickers",
            [],
        ),
        "exact_forecast_refresh_attempted": forecast_refresh_result.get("attempted", 0),
        "exact_forecast_refresh_inserted": forecast_refresh_result.get(
            "forecasts_inserted",
            0,
        ),
        "exact_forecast_refresh_skipped": forecast_refresh_result.get("skipped", 0),
        "exact_forecast_refresh_status": forecast_refresh_result.get("status"),
        "exact_forecast_refresh_tickers": forecast_refresh_result.get(
            "selected_tickers",
            [],
        ),
        "ranking_coverage_repairs_inserted": r7_summary.get("rankings_inserted", 0),
        "ranking_coverage_gap_after_repair": r7_summary.get(
            "missing_or_stale_ranking_rows_after",
        ),
        "stage_duration_seconds": stage_duration_seconds,
        "stage_durations_seconds": stage_duration_seconds,
        "slowest_stage": slowest_stage.get("stage"),
        "slowest_stage_seconds": slowest_stage.get("duration_seconds"),
        "data_freshness_gap_after_refresh": r4_summary.get("primary_gap"),
        "primary_gap_after_refresh": r4_summary.get("primary_gap"),
        "primary_gap_scope": r4_summary.get("primary_gap_scope"),
        "phase3bc_main_blocker": phase3bc_summary.get("main_blocker"),
    }
    summary.update(
        _freshness_backlog_classification(
            summary,
            snapshot_refresh_result=snapshot_refresh_result,
            forecast_refresh_result=forecast_refresh_result,
        )
    )
    summary["primary_gap_after_refresh"] = _actionability_primary_gap_after_refresh(
        summary
    )
    summary["watch_state"] = _watch_state(summary, candidates, preflight_results)
    summary["liquidity_actionability_state"] = _liquidity_actionability_state(summary)
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3BC-R5",
        "phase_version": PHASE3BC_R5_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_FRESHNESS_WATCH_AND_POSITIVE_EV_TRIGGER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "risk_preflight_only": True,
        "model_name": MODEL_NAME,
        "options": options,
        "summary": summary,
        "phase3bc_summary": phase3bc_summary,
        "phase3bc_r3_summary": r3_payload.get("summary", {}),
        "phase3bc_r7_summary": r7_summary,
        "phase3bc_r4_summary": r4_summary,
        "exact_snapshot_refresh_result": exact_snapshot_refresh_result or _empty_snapshot_refresh(),
        "exact_forecast_refresh_result": exact_forecast_refresh_result
        or _empty_exact_forecast_refresh(),
        "stage_timings": stage_timings or [],
        "stage_duration_seconds": stage_duration_seconds,
        "stage_durations_seconds": stage_duration_seconds,
        "preflight_action_counts": dict(sorted(preflight_actions.items())),
        "candidate_blocked_reason_counts": dict(sorted(blocked_reasons.items())),
        "preflight_blocker_counts": preflight_blocker_counts,
        "positive_ev_preflight_candidates": candidates,
        "positive_ev_expired_window_examples": positive_ev_expired_window[:25],
        "positive_ev_no_executable_book_examples": positive_ev_no_executable_book[:25],
        "positive_ev_liquidity_positive_examples": positive_ev_liquidity_positive[:25],
        "positive_ev_clean_book_examples": positive_ev_clean_book[:25],
        "positive_ev_snapshot_stale_examples": positive_ev_snapshot_stale[:25],
        "positive_ev_forecast_stale_examples": positive_ev_forecast_stale[:25],
        "positive_ev_spread_blocked_examples": positive_ev_spread_blocked[:25],
        "positive_ev_clean_book_risk_missing_examples": (
            positive_ev_clean_book_risk_missing[:25]
        ),
        "best_ev_candidates": ev_calibration["best_candidates"],
        "ev_near_miss_examples": ev_calibration["near_miss_examples"],
        "liquidity_watch_rows": liquidity_watch_rows[:50],
        "liquidity_emergence_examples": liquidity_emergence[
            "liquidity_emergence_examples"
        ],
        "positive_ev_liquidity_emergence_examples": liquidity_emergence[
            "positive_ev_liquidity_emergence_examples"
        ],
        "near_miss_clean_book_emergence_examples": liquidity_emergence[
            "near_miss_clean_book_emergence_examples"
        ],
        "phase3m_phase3n_preflight_results": preflight_results,
        "blocked_active_pure_examples": blocked[:50],
        "reports": reports,
        "recommended_next_action": _recommended_next_action(summary),
        "next_commands": _next_commands(options),
    }


def _candidate_block_reason(
    row: dict[str, Any],
    *,
    risk: dict[str, Any] | None,
    freshness_minutes: int,
    now: Any,
) -> str | None:
    if not row.get("active_market"):
        return "inactive_market"
    if row.get("structure_status") != "PURE_CRYPTO":
        return "not_pure_crypto"
    if row.get("readiness_status") != "PAPER_READY_CANDIDATE":
        return str(row.get("readiness_status") or "not_paper_ready")
    if row.get("final_action") != "PAPER_READY_CANDIDATE":
        return "final_action_not_paper_ready"
    if row.get("best_side") not in {BUY_YES, BUY_NO}:
        return "missing_best_side"
    if to_decimal(row.get("best_price")) is None:
        return "missing_best_price"
    expected_value = to_decimal(row.get("expected_value"))
    if expected_value is None or expected_value <= 0:
        return "ev_not_positive"
    snapshot_at = parse_datetime(row.get("latest_snapshot_at"))
    if snapshot_at is None:
        return "snapshot_missing"
    if (now - snapshot_at).total_seconds() / 60 > freshness_minutes:
        return "snapshot_stale"
    forecast_at = parse_datetime(row.get("latest_forecast_at"))
    if forecast_at is None:
        return "forecast_missing"
    if (now - forecast_at).total_seconds() / 60 > freshness_minutes:
        return "forecast_stale"
    ranked_at = parse_datetime(row.get("latest_ranking_at"))
    if ranked_at is None:
        return "ranking_missing"
    if ranked_at < forecast_at:
        return "ranking_before_forecast"
    if (now - ranked_at).total_seconds() / 60 > freshness_minutes:
        return "ranking_stale"
    spread = to_decimal(row.get("spread"))
    if spread is None:
        return "spread_missing"
    liquidity_score = to_decimal(row.get("liquidity_score"))
    if liquidity_score is None:
        return "liquidity_missing"
    if risk is None:
        return None
    decision_at = parse_datetime(risk.get("decision_timestamp"))
    if decision_at is None or decision_at < ranked_at:
        return None
    return "phase3n_risk_current"


def _candidate_preflight_blockers(
    row: dict[str, Any],
    *,
    risk: dict[str, Any] | None,
    freshness_minutes: int,
    now: Any,
) -> list[str]:
    if not _positive_expected_value(row):
        return []
    blockers: list[str] = []
    readiness_status = str(row.get("readiness_status") or "")
    if readiness_status == "WATCH_LOW_SCORE":
        blockers.append(PREFLIGHT_LOW_SCORE)
    if readiness_status == "WATCH_LOW_EDGE":
        blockers.append(PREFLIGHT_LOW_EDGE)

    liquidity_score = to_decimal(row.get("liquidity_score"))
    if liquidity_score is None or liquidity_score <= 0:
        blockers.append(PREFLIGHT_LIQUIDITY_ZERO)

    snapshot_at = parse_datetime(row.get("latest_snapshot_at"))
    if snapshot_at is None or (now - snapshot_at).total_seconds() / 60 > freshness_minutes:
        blockers.append(PREFLIGHT_SNAPSHOT_STALE)

    forecast_at = parse_datetime(row.get("latest_forecast_at"))
    ranked_at = parse_datetime(row.get("latest_ranking_at"))
    if (
        forecast_at is None
        or ranked_at is None
        or ranked_at < forecast_at
        or (now - ranked_at).total_seconds() / 60 > freshness_minutes
    ):
        blockers.append(PREFLIGHT_RANKING_GAP)

    if risk is None:
        blockers.append(PREFLIGHT_RISK_MISSING)
    return _unique(blockers)


def _positive_expected_value(row: dict[str, Any]) -> bool:
    expected_value = to_decimal(row.get("expected_value"))
    return expected_value is not None and expected_value > 0


def _row_has_snapshot_stale_blocker(row: dict[str, Any]) -> bool:
    if str(row.get("freshness_issue") or "") in SNAPSHOT_REFRESH_ISSUES:
        return True
    return PREFLIGHT_SNAPSHOT_STALE in set(row.get("preflight_blockers") or [])


def _row_has_forecast_stale_blocker(row: dict[str, Any]) -> bool:
    freshness_issue = str(row.get("freshness_issue") or "")
    if freshness_issue in {"FORECAST_STALE", "FORECAST_MISSING"}:
        return True
    gates = {str(gate) for gate in row.get("blocking_gates") or []}
    return "forecast_stale" in gates or "forecast_missing" in gates


def _row_has_spread_blocker(row: dict[str, Any]) -> bool:
    gates = {str(gate) for gate in row.get("blocking_gates") or []}
    return "spread_block" in gates


def _row_has_missing_risk(row: dict[str, Any]) -> bool:
    if PREFLIGHT_RISK_MISSING in set(row.get("preflight_blockers") or []):
        return True
    if "phase3n_latest" in row and row.get("phase3n_latest") is None:
        return True
    return str(row.get("phase3n_risk_state") or "") == "MISSING"


def _ev_calibration(
    r4_payload: dict[str, Any],
    *,
    near_miss_band: Decimal,
    now: Any,
) -> dict[str, Any]:
    rows = _current_ev_rows(r4_payload, now=now)
    best_rows = sorted(rows, key=_ev_candidate_sort_key, reverse=True)[:25]
    near_misses = [
        row
        for row in best_rows
        if _near_positive_expected_value(row, near_miss_band=near_miss_band)
    ]
    best = best_rows[0] if best_rows else None
    best_ev = to_decimal(best.get("expected_value")) if best else None
    near_miss_liquidity_positive = [
        row for row in near_misses if _liquidity_positive(row)
    ]
    near_miss_clean_execution = [
        row for row in near_misses if _clean_executable_book(row)
    ]
    positive_rows = [row for row in rows if _positive_expected_value(row)]
    missing_price_rows = [
        row for row in rows if to_decimal(row.get("best_price")) is None
    ]
    return {
        "state": _ev_calibration_state(
            positive_rows=positive_rows,
            near_misses=near_misses,
            best_ev=best_ev,
        ),
        "best_expected_value": decimal_to_str(best_ev),
        "best_expected_value_cents": _cents(best_ev),
        "best_candidate_ticker": best.get("ticker") if best else None,
        "best_gap_to_positive_cents": _gap_to_positive_cents(best_ev),
        "near_miss_rows": len(near_misses),
        "near_miss_liquidity_positive_rows": len(near_miss_liquidity_positive),
        "near_miss_clean_execution_rows": len(near_miss_clean_execution),
        "missing_executable_price_rows": len(missing_price_rows),
        "best_candidates": [_ev_candidate_payload(row) for row in best_rows],
        "near_miss_examples": [_ev_candidate_payload(row) for row in near_misses[:25]],
    }


def _liquidity_watch_rows(
    positive_rows: list[dict[str, Any]],
    ev_calibration: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    for row in positive_rows:
        _merge_watch_row(rows_by_ticker, _liquidity_watch_payload(row))
    for row in ev_calibration.get("near_miss_examples", []) or []:
        if isinstance(row, dict):
            _merge_watch_row(rows_by_ticker, _liquidity_watch_payload(row))
    rows = list(rows_by_ticker.values())
    return sorted(rows, key=_liquidity_watch_sort_key, reverse=True)


def _liquidity_emergence(
    previous_payload: dict[str, Any],
    current_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_by_ticker = _previous_liquidity_watch_rows(previous_payload)
    liquidity_examples: list[dict[str, Any]] = []
    positive_ev_liquidity_examples: list[dict[str, Any]] = []
    near_miss_liquidity_examples: list[dict[str, Any]] = []
    clean_book_examples: list[dict[str, Any]] = []
    positive_ev_clean_book_examples: list[dict[str, Any]] = []
    near_miss_clean_book_examples: list[dict[str, Any]] = []

    for current in current_rows:
        ticker = str(current.get("ticker") or "")
        previous = previous_by_ticker.get(ticker)
        if not previous:
            continue
        liquidity_emerged = _liquidity_crossed_positive(previous, current)
        clean_book_emerged = (
            not _clean_executable_book(previous) and _clean_executable_book(current)
        )
        if not liquidity_emerged and not clean_book_emerged:
            continue
        event = _liquidity_transition_payload(
            previous=previous,
            current=current,
            liquidity_emerged=liquidity_emerged,
            clean_book_emerged=clean_book_emerged,
        )
        if liquidity_emerged:
            liquidity_examples.append(event)
            if _positive_expected_value(current):
                positive_ev_liquidity_examples.append(event)
            elif _near_positive_expected_value(current, near_miss_band=EV_NEAR_MISS_BAND):
                near_miss_liquidity_examples.append(event)
        if clean_book_emerged:
            clean_book_examples.append(event)
            if _positive_expected_value(current):
                positive_ev_clean_book_examples.append(event)
            elif _near_positive_expected_value(current, near_miss_band=EV_NEAR_MISS_BAND):
                near_miss_clean_book_examples.append(event)

    liquidity_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    positive_ev_liquidity_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    near_miss_liquidity_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    clean_book_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    positive_ev_clean_book_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    near_miss_clean_book_examples.sort(key=_liquidity_watch_sort_key, reverse=True)
    return {
        "liquidity_emergence_rows": len(liquidity_examples),
        "positive_ev_liquidity_emergence_rows": len(positive_ev_liquidity_examples),
        "near_miss_liquidity_emergence_rows": len(near_miss_liquidity_examples),
        "clean_execution_emergence_rows": len(clean_book_examples),
        "positive_ev_clean_execution_emergence_rows": len(
            positive_ev_clean_book_examples
        ),
        "near_miss_clean_book_emergence_rows": len(near_miss_clean_book_examples),
        "liquidity_emergence_top_tickers": [
            str(row.get("ticker")) for row in liquidity_examples[:10]
        ],
        "liquidity_emergence_examples": liquidity_examples[:25],
        "positive_ev_liquidity_emergence_examples": (
            positive_ev_liquidity_examples[:25]
        ),
        "near_miss_clean_book_emergence_examples": near_miss_clean_book_examples[:25],
    }


def _previous_liquidity_watch_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    for section in (
        "liquidity_watch_rows",
        "positive_ev_preflight_candidates",
        "positive_ev_no_executable_book_examples",
        "positive_ev_liquidity_positive_examples",
        "positive_ev_clean_book_examples",
        "positive_ev_snapshot_stale_examples",
        "positive_ev_forecast_stale_examples",
        "positive_ev_spread_blocked_examples",
        "positive_ev_clean_book_risk_missing_examples",
        "ev_near_miss_examples",
        "best_ev_candidates",
    ):
        for row in payload.get(section, []) or []:
            if isinstance(row, dict):
                _merge_watch_row(rows_by_ticker, _liquidity_watch_payload(row))
    return rows_by_ticker


def _merge_watch_row(
    rows_by_ticker: dict[str, dict[str, Any]],
    row: dict[str, Any],
) -> None:
    ticker = str(row.get("ticker") or "")
    if not ticker:
        return
    existing = rows_by_ticker.get(ticker)
    if existing is None or _liquidity_watch_sort_key(row) > _liquidity_watch_sort_key(existing):
        rows_by_ticker[ticker] = row


def _liquidity_watch_payload(row: dict[str, Any]) -> dict[str, Any]:
    expected_value = to_decimal(row.get("expected_value"))
    watch_type = "EV_WATCH"
    if expected_value is not None and expected_value > 0:
        watch_type = "POSITIVE_EV"
    elif _near_positive_expected_value(row, near_miss_band=EV_NEAR_MISS_BAND):
        watch_type = "NEAR_MISS"
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title") or row.get("title") or row.get("ticker"),
        "watch_type": watch_type,
        "readiness_status": row.get("readiness_status"),
        "final_action": row.get("final_action"),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "side_probability": row.get("side_probability"),
        "expected_value": decimal_to_str(expected_value),
        "expected_value_cents": row.get("expected_value_cents") or _cents(expected_value),
        "gap_to_positive_cents": row.get("gap_to_positive_cents")
        or _gap_to_positive_cents(expected_value),
        "opportunity_score": row.get("opportunity_score"),
        "liquidity_score": row.get("liquidity_score"),
        "spread": row.get("spread"),
        "freshness_issue": row.get("freshness_issue"),
        "blocking_gates": row.get("blocking_gates") or [],
        "what_would_make_paper_ready": row.get("what_would_make_paper_ready") or [],
        "clean_execution": _clean_executable_book(row),
        "liquidity_positive": _liquidity_positive(row),
    }


def _liquidity_crossed_positive(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    previous_liquidity = to_decimal(previous.get("liquidity_score"))
    current_liquidity = to_decimal(current.get("liquidity_score"))
    return (
        current_liquidity is not None
        and current_liquidity > 0
        and (previous_liquidity is None or previous_liquidity <= 0)
    )


def _liquidity_transition_payload(
    *,
    previous: dict[str, Any],
    current: dict[str, Any],
    liquidity_emerged: bool,
    clean_book_emerged: bool,
) -> dict[str, Any]:
    transition_parts: list[str] = []
    if liquidity_emerged:
        transition_parts.append("Liquidity appeared")
    if clean_book_emerged:
        transition_parts.append("Clean execution appeared")
    return {
        **current,
        "previous_liquidity_score": previous.get("liquidity_score"),
        "previous_spread": previous.get("spread"),
        "previous_clean_execution": previous.get("clean_execution")
        if "clean_execution" in previous
        else _clean_executable_book(previous),
        "liquidity_emerged": liquidity_emerged,
        "clean_book_emerged": clean_book_emerged,
        "transition_label": "; ".join(transition_parts),
    }


def _liquidity_watch_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    spread = to_decimal(row.get("spread"))
    inverse_spread = -spread if spread is not None else Decimal("-999")
    clean_book = Decimal("1") if _clean_executable_book(row) else Decimal("0")
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        clean_book,
        to_decimal(row.get("liquidity_score")) or Decimal("0"),
        inverse_spread,
    )


def _current_ev_rows(r4_payload: dict[str, Any], *, now: Any) -> list[dict[str, Any]]:
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    for section in (
        "top_blocked_rows",
        "no_positive_ev_examples",
        "primary_gap_examples",
        "positive_ev_no_executable_book_examples",
        "positive_ev_clean_book_examples",
    ):
        for row in r4_payload.get(section, []) or []:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            if _diagnostic_row_expired(row, now=now):
                continue
            if row.get("active_market") is False:
                continue
            if row.get("structure_status") not in (None, "PURE_CRYPTO"):
                continue
            rows_by_ticker.setdefault(ticker, row)
    return list(rows_by_ticker.values())


def _diagnostic_row_expired(row: dict[str, Any], *, now: Any) -> bool:
    if row.get("active_window_status") == "EXPIRED":
        return True
    close_time = parse_datetime(row.get("ticker_close_time_utc"))
    return close_time is not None and close_time <= now


def _near_positive_expected_value(
    row: dict[str, Any],
    *,
    near_miss_band: Decimal,
) -> bool:
    expected_value = to_decimal(row.get("expected_value"))
    return expected_value is not None and -near_miss_band <= expected_value <= 0


def _ev_candidate_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    spread = to_decimal(row.get("spread"))
    inverse_spread = -spread if spread is not None else Decimal("-999")
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("liquidity_score")) or Decimal("0"),
        inverse_spread,
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
    )


def _ev_calibration_state(
    *,
    positive_rows: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
    best_ev: Decimal | None,
) -> str:
    if positive_rows:
        return "POSITIVE_EV_AVAILABLE"
    if near_misses:
        return "NEAR_MISS_NO_POSITIVE_EV"
    if best_ev is None:
        return "NO_EV_EVIDENCE"
    return "NEGATIVE_EV_ONLY"


def _gap_to_positive_cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > 0:
        return "0.0"
    return _cents(-value)


def _ev_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    expected_value = to_decimal(row.get("expected_value"))
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title"),
        "readiness_status": row.get("readiness_status"),
        "final_action": row.get("final_action"),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "side_probability": row.get("side_probability"),
        "expected_value": decimal_to_str(expected_value),
        "expected_value_cents": _cents(expected_value),
        "gap_to_positive_cents": _gap_to_positive_cents(expected_value),
        "price_improvement_needed_for_positive_ev": row.get(
            "price_improvement_needed_for_positive_ev"
        ),
        "opportunity_score": row.get("opportunity_score"),
        "liquidity_score": row.get("liquidity_score"),
        "spread": row.get("spread"),
        "freshness_issue": row.get("freshness_issue"),
        "blocking_gates": row.get("blocking_gates") or [],
        "what_would_make_paper_ready": row.get("what_would_make_paper_ready") or [],
    }


def _liquidity_positive(row: dict[str, Any]) -> bool:
    liquidity_score = to_decimal(row.get("liquidity_score"))
    return liquidity_score is not None and liquidity_score > 0


def _no_executable_book(row: dict[str, Any]) -> bool:
    if to_decimal(row.get("best_price")) is None:
        return True
    liquidity_score = to_decimal(row.get("liquidity_score"))
    return liquidity_score is None or liquidity_score <= 0


def _clean_executable_book(row: dict[str, Any]) -> bool:
    liquidity_score = to_decimal(row.get("liquidity_score"))
    spread = to_decimal(row.get("spread"))
    return (
        to_decimal(row.get("best_price")) is not None
        and liquidity_score is not None
        and liquidity_score >= Decimal("30")
        and spread is not None
        and spread <= Decimal("0.02")
    )


def _current_crypto_window(row: dict[str, Any], *, now: Any) -> bool:
    close_time = crypto_ticker_close_time_utc(row.get("ticker"))
    return close_time is None or close_time > now


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _stage_duration_seconds(stage_timings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(row.get("stage")): row.get("duration_seconds")
        for row in stage_timings
        if row.get("stage")
    }


def _slowest_stage(stage_duration_seconds: dict[str, Any]) -> dict[str, Any]:
    pairs = [
        (stage, to_decimal(duration))
        for stage, duration in stage_duration_seconds.items()
        if to_decimal(duration) is not None
    ]
    if not pairs:
        return {"stage": None, "duration_seconds": None}
    stage, duration = max(pairs, key=lambda item: item[1])
    return {
        "stage": stage,
        "duration_seconds": decimal_to_str(duration),
    }


class _StageTimer(AtomicStageHeartbeat):
    def __init__(self, output_dir: Path, *, cycle_number: int, total_cycles: int) -> None:
        super().__init__(
            output_dir / "phase3bc_r5_heartbeat.json",
            phase="3BC-R5",
            metadata={
                "cycle_number": cycle_number,
                "total_cycles": total_cycles,
                "paper_only_safety": PAPER_ONLY_SAFETY,
                "live_or_demo_execution": False,
            },
        )


def _maybe_refresh_exact_snapshots(
    session: Session,
    r4_payload: dict[str, Any],
    *,
    enabled: bool,
    limit: int,
) -> dict[str, Any]:
    if not enabled:
        return _empty_snapshot_refresh(status="DISABLED")
    summary = r4_payload.get("summary") or {}
    if not _should_refresh_exact_snapshots(r4_payload):
        return _empty_snapshot_refresh(status="NOT_APPLICABLE")
    tickers, selection = _snapshot_refresh_selection(
        r4_payload,
        limit=limit,
        now=utc_now(),
    )
    if not tickers:
        result = _empty_snapshot_refresh(status="NO_ACTIVE_OPEN_TICKERS")
        result.update(selection)
        return result
    result = repair_crypto_snapshots_for_tickers(session, tickers, limit=limit)
    true_ranking_gap = int(summary.get("true_ranking_gap_after_repair") or 0)
    result["trigger"] = "R23_EXACT_SNAPSHOT_REFRESH_FOR_ACTIONABLE_CRYPTO_CANDIDATES"
    result["ranking_gaps_did_not_block_refresh"] = true_ranking_gap > 0
    result["positive_ev_priority"] = True
    result["book_visible_priority"] = True
    result["no_book_recheck_priority"] = True
    result["r23_priority"] = True
    result.update(selection)
    return result


def _should_refresh_exact_snapshots(r4_payload: dict[str, Any]) -> bool:
    summary = r4_payload.get("summary") or {}
    snapshot_stale = int(summary.get("snapshot_stale_rows") or 0)
    snapshot_missing = int(summary.get("snapshot_missing_rows") or 0)
    return (
        snapshot_stale > 0
        or snapshot_missing > 0
        or _has_actionable_snapshot_recheck_candidate(r4_payload)
    )


def _has_actionable_snapshot_recheck_candidate(r4_payload: dict[str, Any]) -> bool:
    now = utc_now()
    for row in _snapshot_refresh_candidate_rows(r4_payload):
        if _row_needs_exact_snapshot_refresh(
            row
        ) and _snapshot_candidate_skip_reason(row, now=now) is None:
            return True
    return False


def _snapshot_refresh_tickers(
    r4_payload: dict[str, Any],
    *,
    limit: int,
    now: Any | None = None,
) -> list[str]:
    tickers, _selection = _snapshot_refresh_selection(r4_payload, limit=limit, now=now)
    return tickers


def _snapshot_refresh_selection(
    r4_payload: dict[str, Any],
    *,
    limit: int,
    now: Any | None = None,
) -> tuple[list[str], dict[str, Any]]:
    resolved_now = now or utc_now()
    rows_by_ticker: dict[str, dict[str, Any]] = {}
    for row in _snapshot_refresh_candidate_rows(r4_payload):
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        if _row_needs_exact_snapshot_refresh(row):
            rows_by_ticker.setdefault(ticker, row)
    rows = list(rows_by_ticker.values())
    skip_reason_by_ticker = {
        str(row.get("ticker") or ""): reason
        for row in rows
        if (
            reason := _snapshot_candidate_skip_reason(row, now=resolved_now)
        )
        is not None
    }
    skipped_reasons = Counter(reason for reason in skip_reason_by_ticker.values())
    active_open_rows = [
        row
        for row in rows
        if str(row.get("ticker") or "") not in skip_reason_by_ticker
    ]
    skipped_rows = sum(skipped_reasons.values())
    active_open_rows = sorted(
        active_open_rows,
        key=_snapshot_refresh_sort_key,
        reverse=True,
    )
    tickers = [str(row["ticker"]) for row in active_open_rows[: max(0, limit)]]
    unselected_rows = active_open_rows[max(0, limit) :]
    positive_ev_candidates = [
        row for row in active_open_rows if _positive_expected_value(row)
    ]
    near_miss_candidates = [
        row
        for row in active_open_rows
        if _near_positive_expected_value(row, near_miss_band=EV_NEAR_MISS_BAND)
    ]
    book_visible_candidates = [
        row for row in active_open_rows if _liquidity_positive(row)
    ]
    no_book_recheck_candidates = [
        row
        for row in active_open_rows
        if _positive_expected_value(row) and _no_executable_book(row)
    ]
    clean_execution_candidates = [
        row for row in active_open_rows if _clean_executable_book(row)
    ]
    stale_current_window_maintenance_candidates = [
        row
        for row in active_open_rows
        if _row_has_stale_snapshot_issue(row)
        and not _positive_expected_value(row)
        and not _near_positive_expected_value(row, near_miss_band=EV_NEAR_MISS_BAND)
    ]
    return tickers, {
        "candidate_filter": SNAPSHOT_REFRESH_CANDIDATE_FILTER,
        "active_open_priority": True,
        "pure_crypto_priority": True,
        "positive_or_near_miss_ev_priority": True,
        "stale_current_window_maintenance_priority": True,
        "book_visible_priority": True,
        "no_book_recheck_priority": True,
        "r23_priority": True,
        "candidate_tickers_considered": len(rows),
        "active_open_candidates": len(active_open_rows),
        "positive_ev_candidates": len(positive_ev_candidates),
        "near_miss_candidates": len(near_miss_candidates),
        "book_visible_candidates": len(book_visible_candidates),
        "no_book_recheck_candidates": len(no_book_recheck_candidates),
        "clean_execution_candidates": len(clean_execution_candidates),
        "stale_current_window_maintenance_candidates": len(
            stale_current_window_maintenance_candidates
        ),
        "closed_or_unknown_candidates_skipped": skipped_rows,
        "skip_reason_counts": dict(sorted(skipped_reasons.items())),
        "selected_tickers": tickers[:100],
        "unselected_active_open_candidates": len(unselected_rows),
        "unselected_reason": EXACT_TICKER_NOT_REFRESHED if unselected_rows else None,
        "unselected_tickers": [
            str(row.get("ticker") or "") for row in unselected_rows[:100]
        ],
    }


def _snapshot_refresh_candidate_rows(r4_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in (
        "snapshot_freshness_rows",
        "current_window_diagnostics",
        "primary_gap_examples",
        "snapshot_freshness_examples",
        "top_blocked_rows",
        "positive_ev_no_executable_book_examples",
    ):
        for row in r4_payload.get(section, []) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _row_needs_exact_snapshot_refresh(row: dict[str, Any]) -> bool:
    if _row_has_stale_snapshot_issue(row):
        return True
    if _positive_expected_value(row) and _no_executable_book(row):
        return True
    return _near_positive_expected_value(
        row,
        near_miss_band=EV_NEAR_MISS_BAND,
    ) and _liquidity_positive(row)


def _row_has_stale_snapshot_issue(row: dict[str, Any]) -> bool:
    freshness_issue = str(row.get("freshness_issue") or "")
    return freshness_issue in SNAPSHOT_REFRESH_ISSUES


def _is_active_open_snapshot_candidate(row: dict[str, Any]) -> bool:
    return _snapshot_candidate_skip_reason(row, now=utc_now()) is None


def _snapshot_candidate_skip_reason(row: dict[str, Any], *, now: Any) -> str | None:
    if row.get("active_market") is not True:
        return "NOT_ACTIVE_MARKET"
    if row.get("structure_status") != "PURE_CRYPTO":
        return "NOT_PURE_CRYPTO"
    if not is_active_market_status(row.get("market_status")):
        return "NOT_ACTIVE_OPEN_STATUS"
    if row.get("active_window_status") == "EXPIRED":
        return "EXPIRED_CRYPTO_WINDOW"
    close_time = crypto_ticker_close_time_utc(row.get("ticker"))
    if close_time is not None and close_time <= now:
        return "TICKER_CLOSE_TIME_PASSED"
    if _row_has_stale_snapshot_issue(row):
        return None
    if not (
        _positive_expected_value(row)
        or _near_positive_expected_value(row, near_miss_band=EV_NEAR_MISS_BAND)
    ):
        return "NOT_POSITIVE_OR_NEAR_MISS_EV"
    if not _liquidity_positive(row) and not _positive_expected_value(row):
        return "NO_EXECUTABLE_LIQUIDITY"
    return None


def _snapshot_refresh_sort_key(
    row: dict[str, Any],
) -> tuple[int, int, int, int, Decimal, Decimal, Decimal]:
    expected_value = to_decimal(row.get("expected_value")) or Decimal("-999")
    spread = to_decimal(row.get("spread"))
    inverse_spread = -spread if spread is not None else Decimal("-999")
    score = to_decimal(row.get("opportunity_score")) or Decimal("0")
    return (
        1 if expected_value > 0 else 0,
        1 if _no_executable_book(row) else 0,
        1 if _clean_executable_book(row) else 0,
        1 if _liquidity_positive(row) else 0,
        expected_value,
        inverse_spread,
        score,
    )


def _empty_snapshot_refresh(*, status: str = "NOT_RUN") -> dict[str, Any]:
    return {
        "mode": "PAPER_ONLY_EXACT_TICKER_SNAPSHOT_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "status": status,
        "requested": 0,
        "attempted": 0,
        "repaired": 0,
        "status_counts": {},
        "rows": [],
        "selected_tickers": [],
        "unselected_active_open_candidates": 0,
        "unselected_reason": None,
        "unselected_tickers": [],
    }


def _maybe_refresh_exact_forecasts(
    session: Session,
    exact_snapshot_refresh_result: dict[str, Any],
    *,
    limit: int,
) -> dict[str, Any]:
    repaired_tickers = _repaired_snapshot_tickers(exact_snapshot_refresh_result)
    if not repaired_tickers:
        return _empty_exact_forecast_refresh(status="NO_REPAIRED_SNAPSHOTS")
    selected_tickers = repaired_tickers[: max(0, limit)]
    snapshots = latest_snapshots_for_forecasts(session, selected_tickers)
    now = utc_now()
    current_snapshots = [
        snapshot
        for snapshot in snapshots
        if _snapshot_is_current_crypto_window(snapshot.ticker, now=now)
    ]
    skipped_expired = max(0, len(snapshots) - len(current_snapshots))
    forecast_summary = run_forecast_models(
        session,
        model_name=MODEL_NAME,
        snapshots=current_snapshots,
    )
    return {
        "mode": "PAPER_ONLY_EXACT_TICKER_FORECAST_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "status": "COMPLETE",
        "requested": len(repaired_tickers),
        "attempted": len(current_snapshots),
        "snapshots_scanned": forecast_summary.snapshots_scanned,
        "forecasts_inserted": forecast_summary.forecasts_inserted,
        "skipped": forecast_summary.skipped,
        "skipped_expired_or_closed_snapshots": skipped_expired,
        "selected_tickers": selected_tickers[:100],
        "unselected_tickers": repaired_tickers[max(0, limit) : max(0, limit) + 100],
        "unselected_reason": EXACT_TICKER_NOT_REFRESHED
        if len(repaired_tickers) > max(0, limit)
        else None,
    }


def _repaired_snapshot_tickers(result: dict[str, Any]) -> list[str]:
    tickers: list[str] = []
    for row in result.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() != "repaired":
            continue
        ticker = str(row.get("ticker") or "")
        if ticker:
            tickers.append(ticker)
    if not tickers:
        tickers = [str(ticker) for ticker in result.get("selected_tickers") or [] if ticker]
    return list(dict.fromkeys(tickers))


def _snapshot_is_current_crypto_window(ticker: str | None, *, now: Any) -> bool:
    close_time = crypto_ticker_close_time_utc(ticker)
    return close_time is None or close_time > now


def _empty_exact_forecast_refresh(*, status: str = "NOT_RUN") -> dict[str, Any]:
    return {
        "mode": "PAPER_ONLY_EXACT_TICKER_FORECAST_REFRESH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "status": status,
        "requested": 0,
        "attempted": 0,
        "snapshots_scanned": 0,
        "forecasts_inserted": 0,
        "skipped": 0,
        "skipped_expired_or_closed_snapshots": 0,
        "selected_tickers": [],
        "unselected_tickers": [],
        "unselected_reason": None,
    }


def _freshness_backlog_classification(
    summary: dict[str, Any],
    *,
    snapshot_refresh_result: dict[str, Any],
    forecast_refresh_result: dict[str, Any],
) -> dict[str, Any]:
    snapshot_backlog = int(summary.get("snapshot_stale_rows") or 0) + int(
        summary.get("snapshot_missing_rows") or 0
    )
    forecast_backlog = int(summary.get("forecast_stale_rows") or 0) + int(
        summary.get("forecast_missing_rows") or 0
    )
    snapshot_unselected = int(
        snapshot_refresh_result.get("unselected_active_open_candidates") or 0
    )
    snapshot_status = FRESHNESS_COMPLETE
    if snapshot_backlog > 0:
        snapshot_status = (
            EXACT_TICKER_NOT_REFRESHED
            if snapshot_unselected > 0
            or int(snapshot_refresh_result.get("active_open_candidates") or 0)
            > int(summary.get("exact_snapshot_refresh_selected") or 0)
            else str(snapshot_refresh_result.get("status") or EXACT_TICKER_NOT_REFRESHED)
        )
    forecast_status = FRESHNESS_COMPLETE
    if forecast_backlog > 0:
        forecast_status = (
            FORECAST_REFRESH_PENDING_AFTER_SNAPSHOT_REFRESH
            if int(forecast_refresh_result.get("forecasts_inserted") or 0) > 0
            else FORECAST_REFRESHED_STILL_STALE
        )
    blocks_current_positive_ev = (
        int(summary.get("positive_ev_snapshot_stale_rows") or 0) > 0
        or int(summary.get("positive_ev_forecast_stale_rows") or 0) > 0
    )
    return {
        "snapshot_backlog_rows": snapshot_backlog,
        "snapshot_backlog_status": snapshot_status,
        "snapshot_backlog_complete": snapshot_backlog == 0,
        "snapshot_backlog_classified": snapshot_backlog == 0
        or snapshot_status != FRESHNESS_COMPLETE,
        "forecast_backlog_rows": forecast_backlog,
        "forecast_backlog_status": forecast_status,
        "forecast_backlog_complete": forecast_backlog == 0,
        "forecast_backlog_classified": forecast_backlog == 0
        or forecast_status != FRESHNESS_COMPLETE,
        "freshness_backlog_blocks_current_positive_ev": blocks_current_positive_ev,
        "data_freshness_complete": snapshot_backlog == 0 and forecast_backlog == 0,
        "data_freshness_partial_reason": None
        if snapshot_backlog == 0 and forecast_backlog == 0
        else ";".join(
            reason
            for reason in (snapshot_status, forecast_status)
            if reason != FRESHNESS_COMPLETE
        ),
    }


def _actionability_primary_gap_after_refresh(summary: dict[str, Any]) -> str | None:
    raw_gap = summary.get("data_freshness_gap_after_refresh")
    if int(summary.get("paper_ready_candidates") or 0) > 0:
        return raw_gap
    if int(summary.get("positive_ev_rows") or 0) > 0 and not bool(
        summary.get("freshness_backlog_blocks_current_positive_ev")
    ):
        blocker_counts = summary.get("preflight_blocker_counts") or {}
        if int(blocker_counts.get("LOW_EDGE") or 0) > 0 or int(
            blocker_counts.get("LOW_SCORE") or 0
        ) > 0:
            return LOW_EDGE_OR_SCORE_BLOCK
        if int(summary.get("positive_ev_no_executable_book_rows") or 0) > 0:
            return POSITIVE_EV_NO_EXECUTABLE_BOOK
        if int(summary.get("positive_ev_clean_book_risk_missing_rows") or 0) > 0:
            return RISK_OR_SIZE_BLOCK
    if (
        int(summary.get("positive_ev_rows") or 0) <= 0
        and str(summary.get("phase3bc_main_blocker") or "")
        == "WATCH_NO_POSITIVE_EXPECTED_VALUE"
        and not bool(summary.get("freshness_backlog_blocks_current_positive_ev"))
    ):
        return EV_NOT_POSITIVE
    return raw_gap


def _true_ranking_gap_after_repair(
    *,
    r4_summary: dict[str, Any],
    r7_summary: dict[str, Any],
) -> int:
    r7_value = r7_summary.get("missing_or_stale_ranking_rows_after")
    if r7_value is not None:
        return int(r7_value or 0)
    return int(r4_summary.get("true_ranking_gap_after_repair") or 0)


def _run_risk_preflight(
    session: Session,
    candidates: list[dict[str, Any]],
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        decision = _paper_decision_for_candidate(session, candidate, settings=settings)
        if decision is None:
            results.append(
                {
                    **candidate,
                    "preflight_status": "SKIPPED",
                    "preflight_reason": "latest ranking or forecast fields were unavailable",
                }
            )
            continue
        sized = ensure_paper_decision_sized(session, decision, settings=settings)
        raw = sized.raw_decision_json
        sizing = raw.get("position_sizing_decision") or {}
        risk = raw.get("advanced_risk_decision") or {}
        results.append(
            {
                **candidate,
                "preflight_status": "RECORDED",
                "phase3m_decision_id": raw.get("position_sizing_decision_id"),
                "phase3m_tier": sizing.get("tier"),
                "phase3m_proposed_contracts": sizing.get("proposed_contracts"),
                "phase3m_executed_contracts": sizing.get("executed_contracts"),
                "phase3n_decision_id": raw.get("advanced_risk_decision_id"),
                "phase3n_action": risk.get("action"),
                "phase3n_mode": risk.get("mode"),
                "phase3n_live_candidate_contracts": risk.get("live_candidate_contracts"),
                "phase3n_executed_contracts": risk.get("executed_contracts"),
                "phase3n_reason_codes": risk.get("reason_codes", []),
                "phase3n_hard_blocks": risk.get("hard_blocks", []),
                "paper_decision_quantity_after_preflight": sized.quantity,
                "preflight_reason": sized.reason,
            }
        )
    return results


def _paper_decision_for_candidate(
    session: Session,
    candidate: dict[str, Any],
    *,
    settings: Settings,
) -> PaperDecision | None:
    ticker = str(candidate.get("ticker") or "")
    if not ticker:
        return None
    ranking = session.scalar(
        select(MarketRanking)
        .where(MarketRanking.ticker == ticker, MarketRanking.forecast_model == MODEL_NAME)
        .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.id))
        .limit(1)
    )
    if ranking is None:
        return None
    forecast = _latest_forecast(session, ticker)
    price = to_decimal(ranking.best_price or candidate.get("best_price"))
    probability = to_decimal(ranking.forecast_probability or candidate.get("model_probability"))
    edge = to_decimal(ranking.estimated_edge or candidate.get("estimated_edge"))
    if (
        price is None
        or probability is None
        or edge is None
        or ranking.best_side not in {BUY_YES, BUY_NO}
    ):
        return None
    reason = (
        "Phase 3BC-R5 paper-only positive-EV crypto risk preflight. "
        "No order submission, cancellation, replacement, or live/demo execution."
    )
    return PaperDecision(
        ticker=ticker,
        forecast_id=forecast.id if forecast is not None else None,
        model_name=MODEL_NAME,
        side=ranking.best_side,
        probability=probability,
        market_price=price,
        limit_price=price,
        edge=edge,
        quantity=settings.paper_max_order_quantity,
        reason=reason,
        raw_decision_json={
            "source": PHASE3BC_R5_VERSION,
            "strategy": "paper_edge_v1",
            "risk_preflight_only": True,
            "execution_enabled": False,
            "execution_dry_run": True,
            "ticker": ticker,
            "ranking_id": ranking.id,
            "forecast_id": forecast.id if forecast is not None else None,
            "ranked_at": ranking.ranked_at.isoformat(),
            "readiness_status": candidate.get("readiness_status"),
            "expected_value": candidate.get("expected_value"),
            "opportunity_score": candidate.get("opportunity_score"),
            "side": ranking.best_side,
            "probability": decimal_to_str(probability),
            "market_price": decimal_to_str(price),
            "limit_price": decimal_to_str(price),
            "edge": decimal_to_str(edge),
            "quantity": settings.paper_max_order_quantity,
            "reason": reason,
        },
    )


def _latest_forecast(session: Session, ticker: str) -> Forecast | None:
    return session.scalar(
        select(Forecast)
        .where(Forecast.ticker == ticker, Forecast.model_name == MODEL_NAME)
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _preflight_settings(settings: Settings) -> Settings:
    paper_settings = learning_paper_settings(settings)
    return paper_settings.model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "overnight_run_demo": False,
            "autopilot_dry_run": True,
            "dynamic_position_sizing_mode": "shadow",
            "advanced_risk_engine_mode": "shadow",
        }
    )


def _latest_risk_decisions_by_ticker(
    session: Session,
    tickers: list[str],
) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    seen: dict[str, dict[str, Any]] = {}
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker.in_(tickers))
        .order_by(
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
    )
    for row in rows:
        if row.ticker in seen:
            continue
        seen[row.ticker] = {
            "id": row.id,
            "decision_timestamp": row.decision_timestamp.isoformat(),
            "mode": row.mode,
            "action": row.action,
            "phase_3m_tier": row.phase_3m_tier,
            "phase_3m_proposed_contracts": row.phase_3m_proposed_contracts,
            "live_candidate_contracts": row.live_candidate_contracts,
            "executed_contracts": row.executed_contracts,
            "reason_codes": _decode_list(row.reason_codes_json),
            "hard_blocks": _decode_list(row.hard_blocks_json),
            "limiting_factors": _decode_list(row.limiting_factors_json),
        }
    return seen


def _candidate_payload(
    row: dict[str, Any],
    *,
    risk: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker"),
        "clean_title": row.get("clean_title") or row.get("title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "readiness_status": row.get("readiness_status"),
        "final_action": row.get("final_action"),
        "best_side": row.get("best_side"),
        "best_price": row.get("best_price"),
        "expected_value": row.get("expected_value"),
        "expected_value_cents": _cents(to_decimal(row.get("expected_value"))),
        "estimated_edge": row.get("estimated_edge"),
        "opportunity_score": row.get("opportunity_score"),
        "liquidity_score": row.get("liquidity_score"),
        "spread": row.get("spread"),
        "latest_ranking_at": row.get("latest_ranking_at"),
        "latest_forecast_at": row.get("latest_forecast_at"),
        "latest_snapshot_at": row.get("latest_snapshot_at"),
        "active_market": row.get("active_market"),
        "market_status": row.get("market_status"),
        "phase3n_latest": risk,
    }


def _candidate_sort_key(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    return (
        to_decimal(row.get("expected_value")) or Decimal("-999"),
        to_decimal(row.get("opportunity_score")) or Decimal("0"),
        to_decimal(row.get("estimated_edge")) or Decimal("0"),
    )


def _watch_state(
    summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    preflight_results: list[dict[str, Any]],
) -> str:
    if preflight_results:
        return "POSITIVE_EV_PREFLIGHT_RECORDED"
    if candidates:
        return "POSITIVE_EV_PREFLIGHT_CANDIDATES_FOUND"
    snapshot_backlog = int(summary.get("snapshot_stale_rows") or 0) > 0 or int(
        summary.get("snapshot_missing_rows") or 0
    ) > 0
    if snapshot_backlog and int(summary.get("exact_snapshot_refresh_selected") or 0) > 0:
        return "REFRESH_SNAPSHOTS"
    if int(summary.get("true_ranking_gap_after_repair") or 0) > 0:
        return "REFRESH_RANKINGS"
    if int(summary.get("forecast_stale_rows") or 0) > 0 or int(
        summary.get("forecast_missing_rows") or 0
    ) > 0:
        return "REFRESH_FORECASTS"
    if int(summary.get("positive_ev_no_executable_book_rows") or 0) > 0:
        return "WAITING_FOR_EXECUTABLE_BOOK"
    if snapshot_backlog:
        return "SNAPSHOT_STALE_NO_ACTIONABLE_BOOK"
    if int(summary.get("positive_ev_rows") or 0) > 0:
        return "WAITING_FOR_CLEAN_LIQUIDITY"
    if int(summary.get("no_positive_ev_rows") or 0) > 0:
        return "WAITING_FOR_POSITIVE_EV"
    if int(summary.get("spread_or_liquidity_blocked_rows") or 0) > 0:
        return "WAITING_FOR_EXECUTION_QUALITY"
    if int(summary.get("active_pure_crypto_rows") or 0) == 0:
        return "NO_ACTIVE_PURE_CRYPTO_ROWS"
    return "WATCHING"


def _liquidity_actionability_state(summary: dict[str, Any]) -> str:
    if int(summary.get("positive_ev_preflight_candidates") or 0) > 0:
        return "CLEAN_BOOK_READY_FOR_PREFLIGHT"
    if int(summary.get("positive_ev_clean_book_rows") or 0) > 0:
        return "CLEAN_BOOK_WAITING_FOR_RISK"
    if int(summary.get("positive_ev_liquidity_positive_rows") or 0) > 0:
        return "LIQUIDITY_POSITIVE_BUT_NOT_CLEAN"
    if int(summary.get("positive_ev_no_executable_book_rows") or 0) > 0:
        return "POSITIVE_EV_NO_EXECUTABLE_BOOK"
    if int(summary.get("positive_ev_rows") or 0) > 0:
        return "POSITIVE_EV_NOT_ACTIONABLE"
    return "WAITING_FOR_POSITIVE_EV"


def _recommended_next_action(summary: dict[str, Any]) -> str:
    state = summary["watch_state"]
    if state == "POSITIVE_EV_PREFLIGHT_RECORDED":
        return (
            "Inspect Phase 3M/3N paper-only risk results; do not place orders unless a "
            "separate explicit human approval flow is added later."
        )
    if state == "POSITIVE_EV_PREFLIGHT_CANDIDATES_FOUND":
        return "Enable risk preflight or rerun R5; clean positive-EV rows are waiting."
    if state == "REFRESH_RANKINGS":
        return "Continue the 15-minute R5 loop; repair only true missing/stale ranking rows."
    if state == "REFRESH_SNAPSHOTS":
        return "Refresh exact-ticker crypto snapshots, then rerun forecasts and ranking repair."
    if state == "REFRESH_FORECASTS":
        return "Run crypto_v2 forecasts against fresh snapshots before repairing rankings."
    if state == "WAITING_FOR_EXECUTABLE_BOOK":
        return (
            "Positive-EV crypto rows exist, but their executable book/liquidity is not "
            "available. Keep the 15-minute watch running for liquidity-positive candidates."
        )
    if state == "SNAPSHOT_STALE_NO_ACTIONABLE_BOOK":
        return (
            "Snapshot-stale crypto rows exist, but they are not active pure-crypto "
            "positive/near-miss rows with visible book. Keep watching for actionable "
            "liquidity before spending exact-ticker refresh calls."
        )
    if state == "WAITING_FOR_CLEAN_LIQUIDITY":
        return (
            "Positive-EV crypto rows have some liquidity signal, but are not clean enough "
            "for paper-only preflight yet."
        )
    if state == "WAITING_FOR_POSITIVE_EV":
        if int(summary.get("ev_near_miss_rows") or 0) > 0:
            return (
                "Keep the 15-minute R5 loop running and surface the near-miss EV rows; "
                "do not run paper-only preflight until expected value is strictly positive."
            )
        return (
            "Continue the 15-minute R5 loop until crypto price/model movement creates "
            "positive EV."
        )
    if state == "WAITING_FOR_EXECUTION_QUALITY":
        return "Wait for tighter spreads or better liquidity before any paper-ready preflight."
    return "Continue the bounded crypto watch and review the next R5 report."


def _next_commands(options: dict[str, Any]) -> list[str]:
    return [
        (
            "kalshi-bot phase3bc-r5-crypto-freshness-watch "
            "--refresh-open-markets "
            f"{_snapshot_repair_flag(options)} "
            f"{_forecast_scope_flag(options)} "
            f"{_opportunity_report_flag(options)} "
            f"{_near_money_flag(options)} "
            f"--market-limit {options.get('market_limit', 500)} "
            f"--market-max-pages {options.get('market_max_pages', 2)} "
            "--near-money-per-symbol-limit "
            f"{options.get('near_money_per_symbol_limit', DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT)} "
            "--near-money-window-limit "
            f"{options.get('near_money_window_limit', DEFAULT_NEAR_MONEY_WINDOW_LIMIT)} "
            "--snapshot-fetch-concurrency "
            f"{options.get('snapshot_fetch_concurrency', DEFAULT_SNAPSHOT_FETCH_CONCURRENCY)} "
            f"--crypto-market-scan-limit {options.get('crypto_market_scan_limit', 5000)} "
            f"--crypto-link-limit {options.get('crypto_link_limit', 2000)} "
            f"--forecast-limit {options.get('forecast_limit', 2000)} "
            f"--opportunity-limit {options.get('opportunity_limit', 150)} "
            f"--phase3bc-limit {options.get('phase3bc_limit', 2000)}"
        ),
        "kalshi-bot scheduler-plan --profile crypto-watch",
    ]


def _snapshot_repair_flag(options: dict[str, Any]) -> str:
    return "--repair-snapshots" if options.get("repair_snapshots") else "--diagnose-snapshots"


def _forecast_scope_flag(options: dict[str, Any]) -> str:
    return (
        "--forecast-current-windows-only"
        if options.get("forecast_current_windows_only")
        else "--forecast-all-active-crypto"
    )


def _opportunity_report_flag(options: dict[str, Any]) -> str:
    return (
        "--generate-opportunity-report"
        if options.get("generate_opportunity_report")
        else "--skip-opportunity-report"
    )


def _near_money_flag(options: dict[str, Any]) -> str:
    return "--near-money-only" if options.get("near_money_only") else "--full-strike-ladder"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3BC-R5 Crypto Freshness Watch + Positive-EV Trigger",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Mode: `{payload['mode']}`",
        "- PAPER ONLY: no live/demo execution and no order submission.",
        "- Phase 3M/3N preflight is invoked only for fresh pure-crypto positive-EV rows.",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Stage Timings", ""])
    if payload.get("stage_timings"):
        for row in payload["stage_timings"]:
            lines.append(f"- {row['stage']}: `{row['duration_seconds']}s`")
    else:
        lines.append("- none: `0`")
    lines.extend(["", "## Exact Snapshot Refresh", ""])
    snapshot_refresh = payload.get("exact_snapshot_refresh_result") or {}
    for key in (
        "status",
        "trigger",
        "candidate_filter",
        "candidate_tickers_considered",
        "active_open_candidates",
        "book_visible_candidates",
        "no_book_recheck_candidates",
        "clean_execution_candidates",
        "positive_ev_candidates",
        "near_miss_candidates",
        "requested",
        "attempted",
        "repaired",
        "status_counts",
        "skip_reason_counts",
    ):
        if key in snapshot_refresh:
            lines.append(f"- {key}: `{snapshot_refresh.get(key)}`")
    lines.extend(["", "## Preflight Actions", ""])
    if payload["preflight_action_counts"]:
        for key, value in payload["preflight_action_counts"].items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- none: `0`")
    lines.extend(["", "## Positive-EV Preflight Blockers", ""])
    for key, value in payload.get("preflight_blocker_counts", {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Liquidity Actionability", ""])
    summary = payload["summary"]
    for key in (
        "liquidity_actionability_state",
        "positive_ev_rows",
        "positive_ev_current_actionability_rows",
        "positive_ev_expired_window_rows",
        "positive_ev_no_executable_book_rows",
        "positive_ev_liquidity_positive_rows",
        "positive_ev_clean_book_rows",
        "positive_ev_snapshot_stale_rows",
        "positive_ev_forecast_stale_rows",
        "positive_ev_spread_blocked_rows",
        "positive_ev_clean_book_risk_missing_rows",
        "positive_ev_preflight_candidates",
    ):
        lines.append(f"- {key}: `{summary.get(key, 0)}`")
    lines.extend(
        [
            "",
            "## EV Calibration",
            "",
            f"- ev_calibration_state: `{summary.get('ev_calibration_state')}`",
            f"- ev_gate_cents: `{summary.get('ev_gate_cents')}`",
            f"- ev_near_miss_band_cents: `{summary.get('ev_near_miss_band_cents')}`",
            f"- best_ev_candidate_ticker: `{summary.get('best_ev_candidate_ticker')}`",
            f"- best_current_expected_value_cents: "
            f"`{summary.get('best_current_expected_value_cents')}`",
            f"- best_ev_gap_to_positive_cents: "
            f"`{summary.get('best_ev_gap_to_positive_cents')}`",
            f"- ev_near_miss_rows: `{summary.get('ev_near_miss_rows')}`",
            f"- ev_near_miss_liquidity_positive_rows: "
            f"`{summary.get('ev_near_miss_liquidity_positive_rows')}`",
            f"- ev_near_miss_clean_execution_rows: "
            f"`{summary.get('ev_near_miss_clean_execution_rows')}`",
            f"- liquidity_emergence_rows: "
            f"`{summary.get('liquidity_emergence_rows')}`",
            f"- positive_ev_liquidity_emergence_rows: "
            f"`{summary.get('positive_ev_liquidity_emergence_rows')}`",
            f"- near_miss_liquidity_emergence_rows: "
            f"`{summary.get('near_miss_liquidity_emergence_rows')}`",
            f"- clean_execution_emergence_rows: "
            f"`{summary.get('clean_execution_emergence_rows')}`",
            "",
            "### Best EV Candidates Below/At Gate",
            "",
            "| Ticker | EV cents | Gap cents | Liquidity | Spread | Needed cents | Gates |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload.get("best_ev_candidates", [])[:25]:
        lines.append(
            "| "
            f"{row.get('ticker')} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{row.get('gap_to_positive_cents') or ''} | "
            f"{row.get('liquidity_score') or ''} | "
            f"{row.get('spread') or ''} | "
            f"{row.get('price_improvement_needed_for_positive_ev') or ''} | "
            f"{_cell(', '.join(row.get('blocking_gates') or []))} |"
        )
    if not payload.get("best_ev_candidates"):
        lines.append("| _No current active EV candidates were available._ |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "### Positive-EV Expired Windows",
            "",
            "| Ticker | EV cents | Liquidity | Spread |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in payload.get("positive_ev_expired_window_examples", [])[:25]:
        lines.append(
            "| "
            f"{row.get('ticker')} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{row.get('liquidity_score') or ''} | "
            f"{row.get('spread') or ''} |"
        )
    if not payload.get("positive_ev_expired_window_examples"):
        lines.append("| _No expired positive-EV rows this cycle._ |  |  |  |")
    lines.extend(
        [
            "",
            "### Positive-EV Without Executable Book",
            "",
            "| Ticker | EV cents | Liquidity | Spread | Blockers |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for row in payload.get("positive_ev_no_executable_book_examples", [])[:25]:
        lines.append(
            "| "
            f"{row.get('ticker')} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{row.get('liquidity_score') or ''} | "
            f"{row.get('spread') or ''} | "
            f"{', '.join(row.get('preflight_blockers') or [])} |"
        )
    if not payload.get("positive_ev_no_executable_book_examples"):
        lines.append("| _No positive-EV no-book rows this cycle._ |  |  |  |  |")
    lines.extend(
        [
            "",
            "### Liquidity Emergence Since Previous Cycle",
            "",
            "| Ticker | Transition | EV cents | Liquidity | Previous liquidity | Spread |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("liquidity_emergence_examples", [])[:25]:
        lines.append(
            "| "
            f"{row.get('ticker')} | "
            f"{_cell(str(row.get('transition_label') or 'Liquidity changed'))} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{row.get('liquidity_score') or ''} | "
            f"{row.get('previous_liquidity_score') or ''} | "
            f"{row.get('spread') or ''} |"
        )
    if not payload.get("liquidity_emergence_examples"):
        lines.append("| _No liquidity emergence detected this cycle._ |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Positive-EV Candidates",
            "",
            "| Ticker | EV cents | Score | Side | Price | Phase 3N |",
            "|---|---:|---:|---|---:|---|",
        ]
    )
    for row in payload["positive_ev_preflight_candidates"][:25]:
        lines.append(
            "| "
            f"{row.get('ticker')} | "
            f"{row.get('expected_value_cents') or ''} | "
            f"{row.get('opportunity_score') or ''} | "
            f"{row.get('best_side') or ''} | "
            f"{row.get('best_price') or ''} | "
            f"{'missing/stale' if row.get('phase3n_latest') is None else 'stale'} |"
        )
    if not payload["positive_ev_preflight_candidates"]:
        lines.append("| _No clean positive-EV candidate this cycle._ |  |  |  |  |  |")
    lines.extend(["", "## Preflight Results", ""])
    if payload["phase3m_phase3n_preflight_results"]:
        lines.append("| Ticker | Phase 3M | Phase 3N | Action | Hard blocks |")
        lines.append("|---|---|---|---|---|")
        for row in payload["phase3m_phase3n_preflight_results"][:25]:
            lines.append(
                "| "
                f"{row.get('ticker')} | "
                f"{row.get('phase3m_tier')} / {row.get('phase3m_proposed_contracts')} | "
                f"{row.get('phase3n_mode')} | "
                f"{row.get('phase3n_action')} | "
                f"{_cell(', '.join(row.get('phase3n_hard_blocks') or []))} |"
            )
    else:
        lines.append("No Phase 3M/3N preflight rows were recorded this cycle.")
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


def _history_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": payload["generated_at"],
        "phase_version": payload["phase_version"],
        "summary": payload["summary"],
        "preflight_action_counts": payload["preflight_action_counts"],
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _decode_list(value: str | None) -> list[Any]:
    decoded = decode_json(value)
    if isinstance(decoded, list):
        return decoded
    if decoded in (None, ""):
        return []
    return [decoded]


def _cents(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return decimal_to_str((value * Decimal("100")).quantize(Decimal("0.1")))


def _cell(value: str) -> str:
    return value.replace("|", "\\|")
