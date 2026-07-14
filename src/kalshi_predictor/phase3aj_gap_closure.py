from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.ticker_windows import crypto_ticker_close_time_utc
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
    warn_if_sqlite_on_onedrive,
)
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Feature,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    Settlement,
)
from kalshi_predictor.kalshi.orderbook import usable_bid_ask_book
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.settlement_reconciliation import (
    PAPER_ONLY_SAFETY,
    build_paper_settlement_reconciliation,
)
from kalshi_predictor.phase3aa_r6 import (
    LOCAL_DERIVED_TICKER_PREFIXES,
    build_phase3aa_r6_composite_settlement_resolver,
)
from kalshi_predictor.phase3bb import (
    DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
    build_phase3bb_general_source_availability,
    build_phase3bb_general_source_evidence,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

PHASE_3AJ_GAP_VERSION = "phase_3aj_gap_closure_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase_3aj")
DEFAULT_REPORT_PATH = Path("reports/phase_3aj_report.md")
RAW_EV_COST_BUFFER = Decimal("0")
MIN_EXECUTABLE_LIQUIDITY_SCORE = Decimal("25")
QUOTE_STALE_AFTER_MINUTES = Decimal("15")
UNKNOWN_TOLERANCE = 0
INTEGRITY_CHECK_TIMEOUT_SECONDS = 5.0
WRITER_PROCESS_MARKERS = (
    "phase3bc-r5-crypto-freshness-watch",
    "accelerate-learning",
    "market-data-refresh",
)

FUNNEL_REASONS = (
    "NO_LINK_SAFE_MARKET",
    "NO_FRESH_FEATURES",
    "NO_FORECAST",
    "NO_POSITIVE_RAW_EV",
    "EV_LOST_TO_SPREAD",
    "EV_LOST_TO_COSTS",
    "CONFIDENCE_BELOW_THRESHOLD",
    "SPREAD_TOO_WIDE",
    "LIQUIDITY_TOO_LOW",
    "QUOTE_STALE",
    "EXPIRED_CRYPTO_WINDOW",
    "PHASE_3S_SKIP",
    "PHASE_3M_ZERO_SIZE",
    "PHASE_3N_RISK_BLOCK",
    "DUPLICATE_IDEMPOTENCY_KEY",
    "MARKET_CLOSE_TOO_NEAR",
    "UNSUPPORTED_MARKET_TYPE",
    "WAITING_FOR_SETTLEMENT",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

SOURCE_MAP = {
    "usda": "commodity_advertised_price_source",
    "cushman": "infrastructure_data_center_capacity_source",
    "flightaware": "transportation_flight_cancellation_source",
}


@dataclass(frozen=True)
class Phase3AJArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AJCompositeArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    rows_path: Path


@dataclass(frozen=True)
class Phase3AJReportArtifactSet:
    json_path: Path
    markdown_path: Path


def build_gap_closure_doctor(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    db_url = database_url_from_settings(resolved)
    writer = _writer_contract(db_writer_monitor(settings=resolved))
    db_identity = _database_identity(
        session,
        db_url=db_url,
        settings=resolved,
        check_integrity=writer["safe_to_write"],
        skip_integrity_reason=None if writer["safe_to_write"] else "active_writer",
    )
    return _with_run_metadata(
        {
            "phase": "3AJ",
            "phase_version": PHASE_3AJ_GAP_VERSION,
            "mode": "PAPER_ONLY_GAP_CLOSURE_PREFLIGHT",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "repository": _repository_identity(),
            "runtime": {
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "platform": platform.platform(),
                "package_path": str(Path(__file__).resolve().parent),
                "cwd": str(Path.cwd().resolve()),
            },
            "config": {
                "env_file": str((Path.cwd() / ".env").resolve())
                if (Path.cwd() / ".env").exists()
                else None,
                "database_url": redact_database_url(db_url),
                "kalshi_env": resolved.kalshi_env,
                "db_backend": resolved.db_backend,
            },
            "database": db_identity,
            "writer": writer,
            "watchers": {
                "long_job_status": writer.get("long_job_status"),
                "crypto_watch_status_path": str(
                    Path("reports/phase3bc_r5/phase3bc_r5_status.json")
                ),
            },
            "status": _doctor_status(db_identity, writer),
            "fail_closed": db_identity.get("integrity_ok") is not True or not writer["safe_to_write"],
            "next_action": _doctor_next_action(db_identity, writer),
        },
        db_identity=db_identity,
    )


def write_gap_closure_doctor_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
) -> Phase3AJArtifactSet:
    payload = build_gap_closure_doctor(session, settings=settings)
    return _write_json_md(
        output_dir,
        "gap_closure_doctor",
        payload,
        _render_key_value_markdown("Phase 3AJ Gap Closure Doctor", payload),
    )


def build_paper_trade_funnel(
    session: Session,
    *,
    window_hours: int = 72,
    replay_readonly: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    generated_at = utc_now()
    cutoff = generated_at - timedelta(hours=window_hours)
    rankings = list(
        session.scalars(
            select(MarketRanking)
            .where(MarketRanking.ranked_at >= cutoff)
            .order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.opportunity_score))
            .limit(2000)
        )
    )
    tickers = sorted({row.ticker for row in rankings})
    forecasts = _latest_forecasts(session, tickers)
    snapshots = _latest_snapshots(session, tickers)
    risk = _latest_risk(session, tickers)
    paper_orders = _paper_order_keys(session, tickers)
    rows = [
        _funnel_row(
            ranking,
            forecast=forecasts.get(ranking.ticker),
            snapshot=snapshots.get(ranking.ticker),
            risk=risk.get(ranking.ticker),
            paper_orders=paper_orders,
            now=generated_at,
            settings=resolved,
        )
        for ranking in rankings
    ]
    reason_counts = Counter(row["reason_code"] for row in rows)
    label_counts = Counter(row["decision_label"] for row in rows)
    stages = _funnel_stages(rows)
    unknown_count = reason_counts.get("UNKNOWN_REQUIRES_INVESTIGATION", 0)
    db_identity = _database_identity(session, db_url=database_url_from_settings(resolved), settings=resolved)
    payload = {
        "phase": "3AJ",
        "phase_version": PHASE_3AJ_GAP_VERSION,
        "mode": "PAPER_ONLY_TRADE_FUNNEL_REPLAY"
        if replay_readonly
        else "PAPER_ONLY_TRADE_FUNNEL_DIAGNOSTIC",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "replay_readonly": replay_readonly,
        "window_hours": window_hours,
        "cutoff": cutoff.isoformat(),
        "thresholds": {
            "opportunity_min_edge": str(resolved.opportunity_min_edge),
            "opportunity_min_score": str(resolved.opportunity_min_score),
            "opportunity_max_spread": str(resolved.opportunity_max_spread),
            "opportunity_min_time_to_close_minutes": str(
                resolved.opportunity_min_time_to_close_minutes
            ),
            "min_executable_liquidity_score": str(MIN_EXECUTABLE_LIQUIDITY_SCORE),
            "quote_stale_after_minutes": str(QUOTE_STALE_AFTER_MINUTES),
            "cost_buffer": str(RAW_EV_COST_BUFFER),
        },
        "summary": {
            "rankings_reviewed": len(rows),
            "distinct_markets": len({row["ticker"] for row in rows}),
            "distinct_opportunities": len({row["opportunity_key"] for row in rows}),
            "tradeable_paper_only": label_counts.get("TRADEABLE_PAPER_ONLY", 0),
            "paper_orders_created": 0,
            "paper_fills_created": 0,
            "unknown_requires_investigation": unknown_count,
            "unknown_tolerance": UNKNOWN_TOLERANCE,
            "status": "FAIL_UNKNOWN_REASONS"
            if unknown_count > UNKNOWN_TOLERANCE
            else "OK_EXPLAINED",
            "no_trade_expected": label_counts.get("TRADEABLE_PAPER_ONLY", 0) == 0,
        },
        "stage_counts": stages,
        "reason_counts": {reason: reason_counts.get(reason, 0) for reason in FUNNEL_REASONS},
        "decision_label_counts": dict(sorted(label_counts.items())),
        "domain_breakdown": _counts(rows, "domain"),
        "model_breakdown": _counts(rows, "forecast_model"),
        "top_block_reasons": reason_counts.most_common(10),
        "nearest_misses": _nearest_misses(rows),
        "positive_raw_ev_failed_execution": [
            row
            for row in rows
            if row["raw_ev_positive"] and row["reason_code"] in {
                "EV_LOST_TO_SPREAD",
                "EV_LOST_TO_COSTS",
                "SPREAD_TOO_WIDE",
                "LIQUIDITY_TOO_LOW",
                "QUOTE_STALE",
            }
        ][:10],
        "rows": rows[:500],
        "next_action": _paper_funnel_next_action(rows, reason_counts),
    }
    return _with_run_metadata(payload, db_identity=db_identity)


def write_paper_trade_funnel_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    window_hours: int = 72,
    replay_readonly: bool = False,
    settings: Settings | None = None,
) -> Phase3AJArtifactSet:
    payload = build_paper_trade_funnel(
        session,
        window_hours=window_hours,
        replay_readonly=replay_readonly,
        settings=settings,
    )
    return _write_json_md(
        output_dir,
        "paper_trade_funnel",
        payload,
        _render_paper_funnel_markdown(payload),
    )


def build_composite_settlement_resolve(
    session: Session,
    *,
    paper_only: bool = True,
    legacy_only: bool = True,
    max_records: int = 5,
    apply: bool = False,
    backup_first: bool = False,
) -> dict[str, Any]:
    if apply and not (paper_only and legacy_only and backup_first and max_records > 0):
        raise ValueError(
            "--apply requires --paper-only, --legacy-only, --backup-first, and --max-records > 0."
        )
    base = build_phase3aa_r6_composite_settlement_resolver(
        session,
        write_settlements=apply,
        refresh_components=False,
        component_refresh_limit=None,
        limit=None,
    )
    target_rows = list(base.get("rows") or [])[:max_records]
    rows = [_phase3aj_composite_row(row) for row in target_rows]
    db_identity = _database_identity(session, db_url=database_url_from_settings(), settings=get_settings())
    payload = {
        "phase": "3AJ",
        "phase_version": PHASE_3AJ_GAP_VERSION,
        "mode": "PAPER_ONLY_LEGACY_COMPOSITE_SETTLEMENT_RESOLVER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "paper_only": paper_only,
        "legacy_only": legacy_only,
        "dry_run": not apply,
        "apply": apply,
        "backup_first": backup_first,
        "max_records": max_records,
        "live_or_demo_execution": False,
        "exchange_writes": False,
        "settlement_writes_enabled": apply,
        "summary": {
            "legacy_rows_reviewed": len(rows),
            "resolvable_rows": sum(1 for row in rows if row["classification"] == "RESOLVABLE"),
            "blocked_rows": sum(1 for row in rows if row["classification"] != "RESOLVABLE"),
            "settlements_written": sum(1 for row in target_rows if row.get("local_settlement_written")),
            "target_legacy_rows_known": base.get("summary", {}).get("composite_rows_reviewed", 0),
        },
        "classification_counts": _counts(rows, "classification"),
        "rows": rows,
        "source_phase3aa_r6_summary": base.get("summary", {}),
        "next_action": _composite_next_action(rows),
    }
    return _with_run_metadata(payload, db_identity=db_identity)


def write_composite_settlement_resolve_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    paper_only: bool = True,
    legacy_only: bool = True,
    max_records: int = 5,
    apply: bool = False,
    backup_first: bool = False,
) -> Phase3AJCompositeArtifactSet:
    payload = build_composite_settlement_resolve(
        session,
        paper_only=paper_only,
        legacy_only=legacy_only,
        max_records=max_records,
        apply=apply,
        backup_first=backup_first,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "composite_settlement_apply" if apply else "composite_settlement_dry_run"
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    rows_path = output_dir / f"{stem}_rows.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    rows_path.write_text(json.dumps(payload["rows"], indent=2, sort_keys=True, default=str), encoding="utf-8")
    markdown_path.write_text(_render_composite_markdown(payload), encoding="utf-8")
    return Phase3AJCompositeArtifactSet(output_dir, json_path, markdown_path, rows_path)


def build_source_readiness_report(
    session: Session,
    *,
    phase: str = "3BB-R2",
    sources: list[str] | None = None,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
) -> dict[str, Any]:
    selected = [source.strip().lower() for source in (sources or list(SOURCE_MAP)) if source.strip()]
    availability = build_phase3bb_general_source_availability(
        session,
        evidence_dir=evidence_dir,
        check_source_urls=False,
    )
    evidence = build_phase3bb_general_source_evidence(session, evidence_dir=evidence_dir)
    availability_by_adapter: dict[str, list[dict[str, Any]]] = {}
    for row in availability.get("availability_rows", []):
        availability_by_adapter.setdefault(str(row.get("source_adapter_key")), []).append(row)
    evidence_by_adapter: dict[str, list[dict[str, Any]]] = {}
    for row in evidence.get("evidence_rows", []):
        evidence_by_adapter.setdefault(str(row.get("source_adapter_key")), []).append(row)
    rows = [
        _source_row(
            source,
            adapter=SOURCE_MAP[source],
            availability_rows=availability_by_adapter.get(SOURCE_MAP[source], []),
            evidence_rows=evidence_by_adapter.get(SOURCE_MAP[source], []),
        )
        for source in selected
        if source in SOURCE_MAP
    ]
    db_identity = _database_identity(session, db_url=database_url_from_settings(), settings=get_settings())
    payload = {
        "phase": phase,
        "phase_version": PHASE_3AJ_GAP_VERSION,
        "mode": "PAPER_ONLY_SOURCE_READINESS_GATE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "evidence_dir": str(evidence_dir),
        "summary": {
            "sources_reviewed": len(rows),
            "link_safe_sources": sum(1 for row in rows if row["link_safe"]),
            "forecast_safe_sources": sum(1 for row in rows if row["forecast_safe"]),
            "blocked_sources": sum(1 for row in rows if not row["forecast_safe"]),
        },
        "source_state_counts": _counts(rows, "state"),
        "sources": rows,
        "safety_gate": {
            "writes_links": False,
            "writes_features": False,
            "writes_forecasts": False,
            "ready_for_review_can_link": False,
            "ready_for_review_can_forecast": False,
        },
        "next_action": _source_next_action(rows),
    }
    return _with_run_metadata(payload, db_identity=db_identity)


def write_source_readiness_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    phase: str = "3BB-R2",
    sources: list[str] | None = None,
    evidence_dir: Path = DEFAULT_GENERAL_SOURCE_EVIDENCE_DIR,
) -> Phase3AJArtifactSet:
    payload = build_source_readiness_report(
        session,
        phase=phase,
        sources=sources,
        evidence_dir=evidence_dir,
    )
    return _write_json_md(
        output_dir,
        "source_readiness_3bb_r2",
        payload,
        _render_source_markdown(payload),
    )


def build_golden_trace_report(
    session: Session,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    db_identity = _database_identity(session, db_url=database_url_from_settings(resolved), settings=resolved)
    payload = {
        "phase": "3AJ",
        "phase_version": PHASE_3AJ_GAP_VERSION,
        "mode": "PAPER_ONLY_DETERMINISTIC_GOLDEN_TRACE",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "paper_orders_created": 0,
        "paper_fills_created": 0,
        "trace_source": "fixture_contract_not_live_market_data",
        "status": "DEFINED_FOR_TESTS_AND_OPERATOR_REVIEW",
        "positive_trace": [
            {"stage": "fresh_market_data", "expected": "pass"},
            {"stage": "valid_link_safe_market", "expected": "pass"},
            {"stage": "fresh_features", "expected": "pass"},
            {"stage": "valid_forecast", "expected": "pass"},
            {"stage": "positive_executable_ev", "expected": "pass"},
            {"stage": "confidence_pass", "expected": "pass"},
            {"stage": "liquidity_pass", "expected": "pass"},
            {"stage": "spread_pass", "expected": "pass"},
            {"stage": "phase3s_proceed", "expected": "pass"},
            {"stage": "phase3m_size_proposal", "expected": "pass"},
            {"stage": "phase3n_paper_approval", "expected": "pass"},
            {"stage": "paper_trade_created", "expected": "fixture_only"},
            {"stage": "final_settlement", "expected": "fixture_only"},
            {"stage": "realized_pnl_roi", "expected": "fixture_only"},
            {"stage": "report_evidence", "expected": "pass"},
        ],
        "negative_traces": [
            {"name": "raw_ev_positive_not_executable", "expected_reason": "EV_LOST_TO_SPREAD"},
            {"name": "active_database_writer_blocks_refresh", "expected_reason": "BLOCKED_BY_ACTIVE_WRITER"},
            {"name": "flightaware_review_pending", "expected_reason": "READY_FOR_REVIEW_NOT_LINK_SAFE"},
            {"name": "usda_no_values", "expected_reason": "CONFIGURED_NO_VALUES"},
            {"name": "cushman_no_values", "expected_reason": "CONFIGURED_NO_VALUES"},
            {"name": "composite_requires_human_review", "expected_reason": "REQUIRES_HUMAN_REVIEW"},
        ],
        "next_action": "Use this trace as a fixture contract; do not create paper trades from it directly.",
    }
    return _with_run_metadata(payload, db_identity=db_identity)


def write_golden_trace_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
) -> Phase3AJArtifactSet:
    payload = build_golden_trace_report(session, settings=settings)
    return _write_json_md(
        output_dir,
        "golden_trace",
        payload,
        _render_key_value_markdown("Phase 3AJ Golden Trace", payload),
    )


def build_market_data_refresh_status(
    session: Session,
    *,
    bounded: bool = True,
    max_duration_seconds: int = 120,
    require_no_active_writer: bool = True,
    enqueue_if_writer_active: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    writer = _writer_contract(db_writer_monitor(settings=resolved))
    watermark = _market_data_watermark(session)
    db_identity = _database_identity(session, db_url=database_url_from_settings(resolved), settings=resolved)
    blocked = require_no_active_writer and not writer["safe_to_write"]
    state = "BLOCKED_BY_ACTIVE_WRITER" if blocked else "NO_REFRESH_NEEDED_STATUS_ONLY"
    retry_after = "after active writer finishes" if blocked else None
    payload = {
        "phase": "3AJ",
        "phase_version": PHASE_3AJ_GAP_VERSION,
        "mode": "BOUNDED_MARKET_DATA_REFRESH_GUARD",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "bounded": bounded,
        "max_duration_seconds": max_duration_seconds,
        "require_no_active_writer": require_no_active_writer,
        "enqueue_if_writer_active": enqueue_if_writer_active,
        "state": state,
        "active_writer": writer,
        "retry_after": retry_after,
        "data_watermark": watermark,
        "refresh_started": False,
        "refresh_completed": False,
        "db_writes": False,
        "live_or_demo_execution": False,
        "next_action": (
            "Retry after db-writer-monitor reports safe_to_write=true."
            if blocked
            else "No active writer detected; run the existing bounded ingestion path if an operator requests it."
        ),
    }
    return _with_run_metadata(payload, db_identity=db_identity)


def write_market_data_refresh_status(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    bounded: bool = True,
    max_duration_seconds: int = 120,
    require_no_active_writer: bool = True,
    enqueue_if_writer_active: bool = False,
    settings: Settings | None = None,
) -> Phase3AJArtifactSet:
    payload = build_market_data_refresh_status(
        session,
        bounded=bounded,
        max_duration_seconds=max_duration_seconds,
        require_no_active_writer=require_no_active_writer,
        enqueue_if_writer_active=enqueue_if_writer_active,
        settings=settings,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "market_data_refresh_status.json"
    top_strip_path = output_dir / "top_strip_status.json"
    markdown_path = output_dir / "market_data_refresh_status.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    top_strip = _top_strip_status(payload)
    top_strip_path.write_text(json.dumps(top_strip, indent=2, sort_keys=True, default=str), encoding="utf-8")
    markdown_path.write_text(_render_key_value_markdown("Phase 3AJ Market Data Refresh", payload), encoding="utf-8")
    return Phase3AJArtifactSet(output_dir, json_path, markdown_path)


def write_phase_3aj_report(
    session: Session,
    *,
    output: Path = DEFAULT_REPORT_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    settings: Settings | None = None,
) -> Phase3AJReportArtifactSet:
    resolved = settings or get_settings()
    doctor = build_gap_closure_doctor(session, settings=resolved)
    funnel = build_paper_trade_funnel(session, settings=resolved)
    composite = build_composite_settlement_resolve(session)
    sources = build_source_readiness_report(session)
    market_data = build_market_data_refresh_status(session, settings=resolved)
    golden_trace_artifacts = write_golden_trace_report(session, output_dir=output_dir, settings=resolved)
    golden_trace = build_golden_trace_report(session, settings=resolved)
    payload = _with_run_metadata(
        {
            "phase": "3AJ",
            "phase_version": PHASE_3AJ_GAP_VERSION,
            "mode": "UNIFIED_GAP_CLOSURE_REPORT",
            "paper_only_safety": PAPER_ONLY_SAFETY,
            "doctor_summary": doctor.get("status"),
            "paper_trade_funnel_summary": funnel.get("summary"),
            "composite_settlement_summary": composite.get("summary"),
            "source_readiness_summary": sources.get("summary"),
            "market_data_state": market_data.get("state"),
            "active_writer": market_data.get("active_writer"),
            "golden_trace_summary": {
                "status": golden_trace.get("status"),
                "trace_source": golden_trace.get("trace_source"),
                "artifact": str(golden_trace_artifacts.json_path),
            },
            "phase3w_consumable": _phase3w_consumable(funnel, composite, sources, market_data),
            "live_trading_enabled": False,
            "operator_actions": [
                doctor.get("next_action"),
                funnel.get("next_action"),
                composite.get("next_action"),
                sources.get("next_action"),
                market_data.get("next_action"),
            ],
        },
        db_identity=doctor["database"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase_3aj_report.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    output.write_text(_render_unified_markdown(payload), encoding="utf-8")
    return Phase3AJReportArtifactSet(json_path=json_path, markdown_path=output)


def _funnel_row(
    ranking: MarketRanking,
    *,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    risk: AdvancedRiskDecisionLog | None,
    paper_orders: set[tuple[str, str, int | None]],
    now: Any,
    settings: Settings,
) -> dict[str, Any]:
    price = to_decimal(ranking.best_price)
    probability = to_decimal(forecast.yes_probability if forecast else ranking.forecast_probability)
    side = str(ranking.best_side or "")
    side_probability = None
    if probability is not None:
        side_probability = Decimal("1") - probability if side == BUY_NO else probability
    raw_ev = side_probability - price if side_probability is not None and price is not None else None
    spread = to_decimal(ranking.spread)
    executable_ev = raw_ev - (spread or Decimal("0")) - RAW_EV_COST_BUFFER if raw_ev is not None else None
    quote_age = _age_minutes(snapshot.captured_at, now) if snapshot is not None else None
    book = (
        usable_bid_ask_book(
            decode_json(snapshot.raw_orderbook_json),
            side=side,
            liquidity_score=ranking.liquidity_score,
            min_liquidity_score=MIN_EXECUTABLE_LIQUIDITY_SCORE,
            max_spread=settings.opportunity_max_spread,
        )
        if snapshot is not None and side in {BUY_YES, BUY_NO}
        else None
    )
    crypto_window_close_time = crypto_ticker_close_time_utc(ranking.ticker)
    crypto_window_expired = (
        crypto_window_close_time is not None and crypto_window_close_time <= utc_now()
    )
    reason = _funnel_reason(
        ranking=ranking,
        forecast=forecast,
        snapshot=snapshot,
        risk=risk,
        price=price,
        raw_ev=raw_ev,
        executable_ev=executable_ev,
        spread=spread,
        quote_age=quote_age,
        book=book,
        paper_orders=paper_orders,
        settings=settings,
    )
    return {
        "ticker": ranking.ticker,
        "opportunity_key": f"{ranking.ticker}:{ranking.forecast_model}:{ranking.id}",
        "forecast_model": ranking.forecast_model,
        "domain": _domain_for_ranking(ranking),
        "ranked_at": ranking.ranked_at.isoformat() if ranking.ranked_at else None,
        "snapshot_at": snapshot.captured_at.isoformat() if snapshot else None,
        "forecast_at": forecast.forecasted_at.isoformat() if forecast else None,
        "best_side": side,
        "best_price": decimal_to_str(price),
        "side_probability": decimal_to_str(side_probability),
        "raw_ev": decimal_to_str(raw_ev),
        "executable_ev": decimal_to_str(executable_ev),
        "raw_ev_positive": bool(raw_ev is not None and raw_ev > 0),
        "executable_ev_positive": bool(executable_ev is not None and executable_ev > 0),
        "spread": decimal_to_str(spread),
        "liquidity_score": ranking.liquidity_score,
        "opportunity_score": ranking.opportunity_score,
        "confidence_score": ranking.model_confidence_score,
        "quote_age_minutes": decimal_to_str(quote_age),
        "crypto_window_close_time": (
            crypto_window_close_time.isoformat() if crypto_window_close_time is not None else None
        ),
        "crypto_window_expired": crypto_window_expired,
        "book_state": book.state if book is not None else None,
        "book_reason": book.reason if book is not None else None,
        "phase_3n_action": risk.action if risk is not None else None,
        "reason_code": reason,
        "decision_label": _decision_label(reason),
        "next_action": _reason_next_action(reason),
    }


def _funnel_reason(
    *,
    ranking: MarketRanking,
    forecast: Forecast | None,
    snapshot: MarketSnapshot | None,
    risk: AdvancedRiskDecisionLog | None,
    price: Decimal | None,
    raw_ev: Decimal | None,
    executable_ev: Decimal | None,
    spread: Decimal | None,
    quote_age: Decimal | None,
    book: Any,
    paper_orders: set[tuple[str, str, int | None]],
    settings: Settings,
) -> str:
    if ranking.best_side not in {BUY_YES, BUY_NO} or price is None:
        return "UNSUPPORTED_MARKET_TYPE"
    close_time = crypto_ticker_close_time_utc(ranking.ticker)
    if close_time is not None and close_time <= utc_now():
        return "EXPIRED_CRYPTO_WINDOW"
    if snapshot is None:
        return "NO_FRESH_FEATURES"
    if quote_age is None or quote_age > QUOTE_STALE_AFTER_MINUTES:
        return "QUOTE_STALE"
    if forecast is None:
        return "NO_FORECAST"
    if raw_ev is None or raw_ev <= 0:
        return "NO_POSITIVE_RAW_EV"
    if executable_ev is None or executable_ev <= 0:
        if spread is not None and spread > 0:
            return "EV_LOST_TO_SPREAD"
        return "EV_LOST_TO_COSTS"
    if to_decimal(ranking.model_confidence_score) is not None and (
        to_decimal(ranking.model_confidence_score) or Decimal("0")
    ) < Decimal("40"):
        return "CONFIDENCE_BELOW_THRESHOLD"
    if spread is not None and spread > settings.opportunity_max_spread:
        return "SPREAD_TOO_WIDE"
    if book is not None and book.state in {"NO_EXECUTABLE_BOOK", "THIN_BOOK"}:
        return "LIQUIDITY_TOO_LOW"
    if (to_decimal(ranking.liquidity_score) or Decimal("0")) < MIN_EXECUTABLE_LIQUIDITY_SCORE:
        return "LIQUIDITY_TOO_LOW"
    time_to_close = to_decimal(ranking.time_to_close_minutes)
    if time_to_close is not None and time_to_close < settings.opportunity_min_time_to_close_minutes:
        return "MARKET_CLOSE_TOO_NEAR"
    if (
        ranking.ticker,
        ranking.forecast_model,
        int(ranking.raw_json and decode_json(ranking.raw_json).get("forecast_id") or 0),
    ) in paper_orders:
        return "DUPLICATE_IDEMPOTENCY_KEY"
    if risk is not None and str(risk.action).upper() == "BLOCK":
        return "PHASE_3N_RISK_BLOCK"
    if (to_decimal(ranking.opportunity_score) or Decimal("0")) < settings.opportunity_min_score:
        return "PHASE_3S_SKIP"
    return "PHASE_3N_RISK_BLOCK" if risk is None else "UNKNOWN_REQUIRES_INVESTIGATION"


def _funnel_stages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stage_defs = [
        ("eligible_markets", lambda row: True),
        ("parsed_or_link_safe_markets", lambda row: row["reason_code"] != "NO_LINK_SAFE_MARKET"),
        (
            "fresh_features",
            lambda row: row["reason_code"]
            not in {"NO_LINK_SAFE_MARKET", "NO_FRESH_FEATURES", "QUOTE_STALE", "EXPIRED_CRYPTO_WINDOW"},
        ),
        ("valid_forecasts", lambda row: row["reason_code"] != "NO_FORECAST"),
        ("calibrated_probabilities", lambda row: row["side_probability"] is not None),
        (
            "positive_raw_ev",
            lambda row: row["raw_ev_positive"] and row["reason_code"] != "EXPIRED_CRYPTO_WINDOW",
        ),
        (
            "positive_executable_ev",
            lambda row: row["executable_ev_positive"]
            and row["reason_code"] != "EXPIRED_CRYPTO_WINDOW",
        ),
        ("confidence_pass", lambda row: row["reason_code"] != "CONFIDENCE_BELOW_THRESHOLD"),
        ("liquidity_pass", lambda row: row["reason_code"] != "LIQUIDITY_TOO_LOW"),
        ("spread_pass", lambda row: row["reason_code"] != "SPREAD_TOO_WIDE"),
        ("phase_3s_proceed", lambda row: row["reason_code"] != "PHASE_3S_SKIP"),
        ("phase_3m_nonzero_size", lambda row: row["reason_code"] != "PHASE_3M_ZERO_SIZE"),
        ("phase_3n_approved", lambda row: row["reason_code"] != "PHASE_3N_RISK_BLOCK"),
        ("paper_decision", lambda row: row["decision_label"] == "TRADEABLE_PAPER_ONLY"),
        ("paper_order", lambda row: False),
        ("paper_fill", lambda row: False),
    ]
    total = len(rows)
    return [
        {
            "stage": name,
            "input_count": total,
            "pass_count": sum(1 for row in rows if predicate(row)),
            "fail_count": total - sum(1 for row in rows if predicate(row)),
        }
        for name, predicate in stage_defs
    ]


def _phase3aj_composite_row(row: dict[str, Any]) -> dict[str, Any]:
    blocked = row.get("blocked_reason")
    classification = "RESOLVABLE" if row.get("ready_to_write") else _composite_classification(blocked, row)
    return {
        "paper_order_ids": row.get("paper_order_ids", []),
        "ticker": row.get("ticker"),
        "classification": classification,
        "human_review_required": classification != "RESOLVABLE",
        "reason_code": blocked or classification,
        "settlement_rule_version": "phase3aj_all_selected_component_sides_win_v1",
        "component_markets": row.get("component_evidence", []),
        "computed_payout": row.get("derived_yes_settlement_value"),
        "realized_pnl": None,
        "roi": None,
        "confidence_level": "HIGH" if classification == "RESOLVABLE" else "BLOCKED",
        "idempotency_key": f"phase3aj:{row.get('ticker')}:v1",
        "ledger_mutation_preview": {
            "before": {"settlement_found": False},
            "after": {
                "settlement_result": row.get("derived_result"),
                "yes_settlement_value": row.get("derived_yes_settlement_value"),
            },
            "writes_on_dry_run": False,
        },
    }


def _composite_classification(blocked: Any, row: dict[str, Any]) -> str:
    if row.get("classification") == "ALREADY_HAS_EXACT_LOCAL_SETTLEMENT":
        return "ALREADY_RESOLVED"
    return {
        "MISSING_COMPONENT_SETTLEMENTS": "WAITING_FOR_COMPONENT_OUTCOME",
        "MARKET_PAYLOAD_MISSING": "MISSING_COMPONENT_MARKET",
        "MVE_SELECTED_LEGS_MISSING": "MISSING_SETTLEMENT_RULE",
        "MVE_SELECTED_LEGS_EMPTY": "MISSING_SETTLEMENT_RULE",
        "INVALID_COMPONENT_MAPPING": "AMBIGUOUS_COMPONENT_MAPPING",
        "COMPONENT_OUTCOME_NOT_BINARY": "WAITING_FOR_COMPONENT_OUTCOME",
        "COMPONENT_SIDE_UNSUPPORTED": "MISSING_SETTLEMENT_RULE",
    }.get(str(blocked), "REQUIRES_HUMAN_REVIEW")


def _source_row(
    source: str,
    *,
    adapter: str,
    availability_rows: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    has_values = any(row.get("availability_status") == "SOURCE_VALUE_AVAILABLE_FOR_REVIEW" for row in availability_rows)
    configured = bool(availability_rows or evidence_rows)
    ready_for_review = has_values or source == "flightaware"
    state = "READY_FOR_REVIEW" if ready_for_review else ("CONFIGURED_NO_VALUES" if configured else "NOT_CONFIGURED")
    if source == "flightaware" and not has_values:
        state = "READY_FOR_REVIEW"
    gates = {
        "can_fetch": configured,
        "has_values": has_values,
        "parser_valid": bool(evidence_rows),
        "reviewer_approved": False,
        "link_safe": False,
        "forecast_safe": False,
        "freshness_ok": has_values,
        "provenance_ok": has_values,
        "point_in_time_safe": False,
    }
    return {
        "source": source,
        "source_adapter_key": adapter,
        "state": state,
        **gates,
        "configuration_state": "configured" if configured else "not_configured",
        "credential_requirement": "configured externally; secrets redacted",
        "row_count": len(availability_rows),
        "value_count": sum(1 for row in availability_rows if row.get("availability_status") == "SOURCE_VALUE_AVAILABLE_FOR_REVIEW"),
        "review_status": "pending" if ready_for_review else "not_ready",
        "blockers": _source_blockers(source, gates, state),
        "exact_next_operator_action": _source_action(source, state),
        "sample_redacted_value_shape": _redacted_shape(availability_rows[:1] or evidence_rows[:1]),
        "availability_status_counts": _counts(availability_rows, "availability_status"),
    }


def _database_identity(
    session: Session,
    *,
    db_url: str,
    settings: Settings,
    check_integrity: bool = False,
    skip_integrity_reason: str | None = None,
) -> dict[str, Any]:
    sqlite_path = sqlite_path_from_url(db_url)
    identity: dict[str, Any] = {
        "database_url": redact_database_url(db_url),
        "sqlite_path": str(sqlite_path) if sqlite_path else None,
        "sqlite_on_onedrive_warning": warn_if_sqlite_on_onedrive(settings, db_url),
        "fingerprint": _database_fingerprint(sqlite_path),
        "integrity_check": None,
        "integrity_ok": None,
        "integrity_check_status": "not_requested",
        "integrity_check_elapsed_seconds": None,
        "integrity_check_timeout_seconds": INTEGRITY_CHECK_TIMEOUT_SECONDS,
        "alembic_revision": None,
    }
    if sqlite_path is None or str(sqlite_path) == ":memory:":
        identity["integrity_ok"] = True
        identity["integrity_check_status"] = "not_applicable"
        return identity
    if skip_integrity_reason:
        identity["integrity_check"] = f"SKIPPED: {skip_integrity_reason}"
        identity["integrity_check_status"] = f"skipped_{skip_integrity_reason}"
    elif check_integrity:
        identity.update(_bounded_sqlite_integrity_check(session))
    else:
        identity["integrity_check"] = "not requested by this bounded report"
    try:
        identity["alembic_revision"] = session.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar()
    except Exception:
        identity["alembic_revision"] = None
    return identity


def _bounded_sqlite_integrity_check(session: Session) -> dict[str, Any]:
    started = time.monotonic()
    driver_connection = _sqlite_driver_connection(session)
    if not hasattr(driver_connection, "set_progress_handler"):
        return _session_integrity_check(session, started=started)

    def interrupt_when_slow() -> int:
        return int(time.monotonic() - started >= INTEGRITY_CHECK_TIMEOUT_SECONDS)

    try:
        driver_connection.set_progress_handler(interrupt_when_slow, 10_000)
        cursor = driver_connection.execute("PRAGMA integrity_check")
        row = cursor.fetchone()
        integrity = row[0] if row else None
        elapsed = round(time.monotonic() - started, 3)
        return {
            "integrity_check": integrity,
            "integrity_ok": integrity == "ok",
            "integrity_check_status": "completed",
            "integrity_check_elapsed_seconds": elapsed,
        }
    except Exception as exc:  # noqa: BLE001 - diagnostic path must report any backend failure.
        elapsed = round(time.monotonic() - started, 3)
        message = str(exc)
        if "interrupted" in message.lower():
            return {
                "integrity_check": (
                    f"TIMEOUT after {INTEGRITY_CHECK_TIMEOUT_SECONDS:g}s; "
                    "full PRAGMA integrity_check did not complete"
                ),
                "integrity_ok": False,
                "integrity_check_status": "timeout",
                "integrity_check_elapsed_seconds": elapsed,
            }
        return {
            "integrity_check": f"ERROR: {exc}",
            "integrity_ok": False,
            "integrity_check_status": "error",
            "integrity_check_elapsed_seconds": elapsed,
        }
    finally:
        try:
            driver_connection.set_progress_handler(None, 0)
        except Exception:
            pass


def _session_integrity_check(session: Session, *, started: float) -> dict[str, Any]:
    try:
        integrity = session.execute(text("PRAGMA integrity_check")).scalar()
    except Exception as exc:  # noqa: BLE001 - report diagnostic, do not hide failure.
        return {
            "integrity_check": f"ERROR: {exc}",
            "integrity_ok": False,
            "integrity_check_status": "error",
            "integrity_check_elapsed_seconds": round(time.monotonic() - started, 3),
        }
    return {
        "integrity_check": integrity,
        "integrity_ok": integrity == "ok",
        "integrity_check_status": "completed",
        "integrity_check_elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _sqlite_driver_connection(session: Session) -> Any:
    raw_connection = session.connection().connection
    driver_connection = getattr(raw_connection, "driver_connection", None)
    if driver_connection is not None:
        return driver_connection
    return getattr(raw_connection, "connection", raw_connection)


def _database_fingerprint(path: Path | None) -> dict[str, Any]:
    if path is None or str(path) == ":memory:":
        return {"kind": "non_sqlite_or_memory", "sha256": None}
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"kind": "missing", "path": str(resolved), "sha256": None}
    stat = resolved.stat()
    sample = f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    return {
        "kind": "sqlite_file_stat",
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(sample).hexdigest(),
    }


def _writer_contract(payload: dict[str, Any]) -> dict[str, Any]:
    process_guard = _active_writer_process()
    active = bool(payload.get("current_writer_pid")) or process_guard is not None
    writer_command = payload.get("current_writer_command") or (
        process_guard.get("command") if process_guard else None
    )
    writer_pid = payload.get("current_writer_pid") or (
        process_guard.get("pid") if process_guard else None
    )
    raw_status = payload.get("status") if payload.get("current_writer_pid") else None
    if raw_status is None and process_guard is not None:
        raw_status = "PROCESS_ACTIVE_NO_DB_LOCK"
    safe_to_write = bool(payload.get("safe_to_start_write")) and process_guard is None
    return {
        "active_writer": active,
        "writer_name": _writer_name(str(writer_command or "")),
        "pid": writer_pid,
        "writer_command": writer_command,
        "started_at": None,
        "last_heartbeat_at": (payload.get("long_job_status") or {}).get("heartbeat", {}).get("updated_at"),
        "lock_age_seconds": payload.get("current_writer_elapsed_seconds"),
        "safe_to_write": safe_to_write,
        "recommended_action": (
            f"Wait for writer process pid {writer_pid} to finish, then rerun db-writer-monitor."
            if process_guard is not None
            else payload.get("recommended_next_action")
        ),
        "long_job_status": payload.get("long_job_status"),
        "raw_status": raw_status,
        "process_guard": process_guard,
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


def _repository_identity() -> dict[str, Any]:
    root = _repo_root()
    branch = _git(["branch", "--show-current"], cwd=root)
    commit = _git(["rev-parse", "HEAD"], cwd=root)
    return {
        "root": str(root),
        "git_branch": branch,
        "git_commit": commit,
        "git_available": commit is not None,
    }


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd().resolve()


def _git(args: list[str], *, cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text_value = result.stdout.strip()
    return text_value or None


def _with_run_metadata(payload: dict[str, Any], *, db_identity: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("generated_at", utc_now().isoformat())
    payload.setdefault("paper_only_safety", PAPER_ONLY_SAFETY)
    payload.setdefault("database_fingerprint", db_identity.get("fingerprint"))
    payload.setdefault("git_commit", _repository_identity().get("git_commit"))
    payload.setdefault("command", _command_context())
    payload.setdefault("command_args", sys.argv[1:])
    payload.setdefault("data_watermark", None)
    return payload


def _command_context() -> str:
    return " ".join(str(part) for part in sys.argv if part)


def _write_json_md(
    output_dir: Path,
    stem: str,
    payload: dict[str, Any],
    markdown: str,
) -> Phase3AJArtifactSet:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")
    return Phase3AJArtifactSet(output_dir, json_path, markdown_path)


def _latest_forecasts(session: Session, tickers: list[str]) -> dict[str, Forecast]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(Forecast)
        .where(Forecast.ticker.in_(tickers))
        .order_by(Forecast.ticker, desc(Forecast.forecasted_at), desc(Forecast.id))
    )
    result: dict[str, Forecast] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


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


def _latest_risk(session: Session, tickers: list[str]) -> dict[str, AdvancedRiskDecisionLog]:
    if not tickers:
        return {}
    rows = session.scalars(
        select(AdvancedRiskDecisionLog)
        .where(AdvancedRiskDecisionLog.ticker.in_(tickers))
        .order_by(
            AdvancedRiskDecisionLog.ticker,
            desc(AdvancedRiskDecisionLog.decision_timestamp),
            desc(AdvancedRiskDecisionLog.id),
        )
    )
    result: dict[str, AdvancedRiskDecisionLog] = {}
    for row in rows:
        result.setdefault(row.ticker, row)
    return result


def _paper_order_keys(session: Session, tickers: list[str]) -> set[tuple[str, str, int | None]]:
    if not tickers:
        return set()
    return {
        (row.ticker, row.model_name, row.forecast_id)
        for row in session.scalars(select(PaperOrder).where(PaperOrder.ticker.in_(tickers)))
    }


def _age_minutes(value: Any, now: Any) -> Decimal | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=now.tzinfo)
    return Decimal(str(max((now - value).total_seconds(), 0))) / Decimal("60")


def _domain_for_ranking(ranking: MarketRanking) -> str:
    series = str(ranking.series_ticker or ranking.ticker or "").lower()
    model = str(ranking.forecast_model or "").lower()
    title = str(ranking.title or "").lower()
    if "crypto" in model or series.startswith(("kxbtc", "kxeth", "kxdoge", "kxxrp", "kxsol")):
        return "crypto"
    if "sports" in model or any(word in title for word in ("game", "team", "match")):
        return "sports"
    if "economic" in model:
        return "economic"
    return "general"


def _decision_label(reason: str) -> str:
    if reason in {"LIQUIDITY_TOO_LOW", "SPREAD_TOO_WIDE", "EV_LOST_TO_SPREAD"}:
        return "INTERESTING_BUT_NOT_EXECUTABLE"
    if reason == "PHASE_3N_RISK_BLOCK":
        return "POSITIVE_EV_RISK_BLOCKED"
    if reason == "NO_POSITIVE_RAW_EV":
        return "NO_SIGNAL"
    if reason == "UNKNOWN_REQUIRES_INVESTIGATION":
        return "TRADEABLE_PAPER_ONLY"
    return "NO_SIGNAL"


def _reason_next_action(reason: str) -> str:
    return {
        "NO_LINK_SAFE_MARKET": "Keep row out of paper trading until exact link/source safety exists.",
        "NO_FRESH_FEATURES": "Refresh exact market snapshots/features before evaluating EV.",
        "NO_FORECAST": "Run the relevant forecast model after features are fresh.",
        "NO_POSITIVE_RAW_EV": "Wait for model or price movement; do not force a paper trade.",
        "EV_LOST_TO_SPREAD": "Wait for spread to tighten enough that EV remains executable.",
        "EV_LOST_TO_COSTS": "Wait for executable EV after configured costs.",
        "CONFIDENCE_BELOW_THRESHOLD": "Wait for confidence to clear the configured threshold.",
        "SPREAD_TOO_WIDE": "Wait for spread to tighten.",
        "LIQUIDITY_TOO_LOW": "Wait for visible executable bid/ask depth and liquidity.",
        "QUOTE_STALE": "Refresh exact snapshots/orderbooks.",
        "EXPIRED_CRYPTO_WINDOW": "Do not refresh or trade; the encoded crypto window is closed.",
        "PHASE_3S_SKIP": "Keep skipped until ROI policy allows proceed.",
        "PHASE_3M_ZERO_SIZE": "Do not trade; sizing returned zero contracts.",
        "PHASE_3N_RISK_BLOCK": "Do not trade; risk gate blocked or has not approved.",
        "DUPLICATE_IDEMPOTENCY_KEY": "Do not duplicate an existing paper order.",
        "MARKET_CLOSE_TOO_NEAR": "Avoid new paper entries too close to settlement.",
        "UNSUPPORTED_MARKET_TYPE": "Add parser/support before routing this market.",
        "WAITING_FOR_SETTLEMENT": "Let settlement watch resolve existing exposure first.",
        "UNKNOWN_REQUIRES_INVESTIGATION": "Investigate why a row reached the end of the funnel.",
    }[reason]


def _nearest_misses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sortable = [
        row
        for row in rows
        if row["reason_code"] != "UNKNOWN_REQUIRES_INVESTIGATION"
    ]
    sortable.sort(
        key=lambda row: (
            to_decimal(row.get("raw_ev")) or Decimal("-999"),
            to_decimal(row.get("opportunity_score")) or Decimal("0"),
        ),
        reverse=True,
    )
    return sortable[:10]


def _paper_funnel_next_action(rows: list[dict[str, Any]], reasons: Counter[str]) -> str:
    if reasons.get("UNKNOWN_REQUIRES_INVESTIGATION", 0):
        return "Investigate unknown funnel exits before any paper-learning run."
    if reasons.get("LIQUIDITY_TOO_LOW", 0) or reasons.get("EV_LOST_TO_SPREAD", 0):
        return "Keep the crypto watch running until EV, spread, and executable depth line up."
    if not rows:
        return "Run forecasts/rankings first; no recent ranking rows were available."
    return "No paper trade is expected from the current evidence; continue bounded refresh/watch loops."


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "none")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _composite_next_action(rows: list[dict[str, Any]]) -> str:
    if any(row["classification"] == "RESOLVABLE" for row in rows):
        return "Review dry-run evidence; apply requires --apply --backup-first."
    if rows:
        return "Keep these legacy rows unresolved until component mapping and outcomes are complete."
    return "No legacy composite/local rows were selected by the guarded resolver."


def _source_blockers(source: str, gates: dict[str, bool], state: str) -> list[str]:
    blockers: list[str] = []
    if not gates["has_values"] and source in {"usda", "cushman"}:
        blockers.append("source values unavailable")
    if source == "flightaware" and not gates["reviewer_approved"]:
        blockers.append("review approval missing")
    if not gates["link_safe"]:
        blockers.append("not link-safe")
    if not gates["forecast_safe"]:
        blockers.append("not forecast-safe")
    if state == "NOT_CONFIGURED":
        blockers.append("source not configured")
    return blockers


def _source_action(source: str, state: str) -> str:
    if source == "usda":
        return "Wait for USDA values, then run fixture/parser review before link or forecast writes."
    if source == "cushman":
        return "Wait for Cushman values/file availability, then run parser review before link or forecast writes."
    if source == "flightaware":
        return "Record explicit review approval plus mapping and point-in-time tests before promotion."
    return f"Review source state {state}."


def _redacted_shape(rows: list[dict[str, Any]]) -> dict[str, str]:
    if not rows:
        return {}
    return {key: type(value).__name__ for key, value in rows[0].items() if "secret" not in key.lower()}


def _source_next_action(rows: list[dict[str, Any]]) -> str:
    if any(row["source"] in {"usda", "cushman"} and not row["has_values"] for row in rows):
        return "Do not link or forecast general candidates until source values are available."
    if any(row["source"] == "flightaware" and not row["reviewer_approved"] for row in rows):
        return "Keep FlightAware ready-for-review only; do not promote to link-safe or forecast-safe."
    return "All selected sources still need explicit promotion gates before downstream use."


def _market_data_watermark(session: Session) -> dict[str, Any]:
    latest = session.scalar(select(func.max(MarketSnapshot.captured_at)))
    now = utc_now()
    age = _age_minutes(latest, now) if latest is not None else None
    state = "MISSING" if latest is None else ("STALE" if age and age > QUOTE_STALE_AFTER_MINUTES else "FRESH")
    return {
        "latest_market_snapshot_at": latest.isoformat() if latest else None,
        "age_minutes": decimal_to_str(age),
        "freshness_threshold_minutes": str(QUOTE_STALE_AFTER_MINUTES),
        "state": state,
    }


def _top_strip_status(refresh_payload: dict[str, Any]) -> dict[str, Any]:
    watermark = refresh_payload["data_watermark"]
    writer = refresh_payload["active_writer"]
    return {
        "state": refresh_payload["state"],
        "market_data_state": watermark["state"],
        "data_watermark": watermark["latest_market_snapshot_at"],
        "staleness_age_minutes": watermark["age_minutes"],
        "freshness_threshold_minutes": watermark["freshness_threshold_minutes"],
        "active_writer": writer["active_writer"],
        "active_writer_pid": writer["pid"],
        "blocked_reason": refresh_payload["state"] if writer["active_writer"] else None,
        "next_retry_time": refresh_payload.get("retry_after"),
        "database_fingerprint": refresh_payload.get("database_fingerprint"),
        "environment_mode": "paper/demo/read-only",
    }


def _phase3w_consumable(
    funnel: dict[str, Any],
    composite: dict[str, Any],
    sources: dict[str, Any],
    market_data: dict[str, Any],
) -> bool:
    return (
        funnel["summary"]["unknown_requires_investigation"] == 0
        and composite["summary"]["legacy_rows_reviewed"] >= 0
        and sources["summary"]["sources_reviewed"] > 0
        and market_data["state"] != "UNKNOWN"
    )


def _doctor_status(db_identity: dict[str, Any], writer: dict[str, Any]) -> str:
    if not writer["safe_to_write"]:
        return "BLOCKED_ACTIVE_WRITER"
    if db_identity.get("integrity_check_status") == "timeout":
        return "INTEGRITY_CHECK_TIMED_OUT"
    if not db_identity.get("integrity_ok"):
        return "BLOCKED_DATABASE_INTEGRITY"
    return "OK"


def _doctor_next_action(db_identity: dict[str, Any], writer: dict[str, Any]) -> str:
    if not writer["safe_to_write"]:
        return "Wait for active writer to finish, then rerun the bounded integrity check before write-capable commands."
    if db_identity.get("integrity_check_status") == "timeout":
        return "Run a dedicated DB health check or retry during an idle window; keep write-capable commands blocked."
    if not db_identity.get("integrity_ok"):
        return "Stop repair and inspect SQLite integrity before any write-capable command."
    return "Safe to run read-only diagnostics; write-capable commands still require explicit flags."


def _writer_name(command: str) -> str | None:
    lowered = command.lower()
    if "phase3bc-r5" in lowered or "crypto" in lowered:
        return "crypto_watcher"
    if "settlement" in lowered:
        return "settlement_watcher"
    if command:
        return "unknown_writer"
    return None


def _render_key_value_markdown(title: str, payload: dict[str, Any]) -> str:
    lines = [f"# {title}", "", f"- Generated at: `{payload.get('generated_at')}`", f"- Safety: `{payload.get('paper_only_safety')}`"]
    for key in ("status", "state", "next_action"):
        if key in payload:
            lines.append(f"- {key}: `{payload[key]}`")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, indent=2, sort_keys=True, default=str)[:6000])
    lines.append("```")
    return "\n".join(lines)


def _render_paper_funnel_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Phase 3AJ Paper Trade Funnel", "", f"- Generated at: `{payload['generated_at']}`", f"- Safety: `{payload['paper_only_safety']}`", f"- Status: `{payload['summary']['status']}`", ""]
    lines.append("## Stage Counts")
    for stage in payload["stage_counts"]:
        lines.append(f"- {stage['stage']}: `{stage['pass_count']}` pass / `{stage['fail_count']}` fail")
    lines.append("")
    lines.append("## Top Reasons")
    for reason, count in payload["top_block_reasons"]:
        lines.append(f"- {reason}: `{count}`")
    lines.append("")
    lines.append(f"Next action: {payload['next_action']}")
    return "\n".join(lines)


def _render_composite_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Phase 3AJ Composite Settlement Resolve", "", f"- Generated at: `{payload['generated_at']}`", f"- Dry run: `{payload['dry_run']}`", f"- Safety: `{payload['paper_only_safety']}`", ""]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append(f"Next action: {payload['next_action']}")
    return "\n".join(lines)


def _render_source_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Phase 3AJ Source Readiness", "", f"- Generated at: `{payload['generated_at']}`", f"- Safety: `{payload['paper_only_safety']}`", ""]
    for row in payload["sources"]:
        lines.append(f"- {row['source']}: `{row['state']}` link_safe=`{row['link_safe']}` forecast_safe=`{row['forecast_safe']}`")
    lines.append("")
    lines.append(f"Next action: {payload['next_action']}")
    return "\n".join(lines)


def _render_unified_markdown(payload: dict[str, Any]) -> str:
    lines = ["# Phase 3AJ Gap Closure Report", "", f"- Generated at: `{payload['generated_at']}`", f"- Safety: `{payload['paper_only_safety']}`", "- Live trading: `disabled`", ""]
    lines.extend(
        [
            "## Summary",
            "",
            f"- Doctor status: `{payload['doctor_summary']}`",
            f"- Paper funnel status: `{payload['paper_trade_funnel_summary']['status']}`",
            f"- Composite rows reviewed: `{payload['composite_settlement_summary']['legacy_rows_reviewed']}`",
            f"- Source blocked: `{payload['source_readiness_summary']['blocked_sources']}`",
            f"- Market data state: `{payload['market_data_state']}`",
            f"- Golden trace: `{payload['golden_trace_summary']['status']}`",
            "",
            "## Operator Actions",
            "",
        ]
    )
    for action in payload["operator_actions"]:
        if action:
            lines.append(f"- {action}")
    return "\n".join(lines)
