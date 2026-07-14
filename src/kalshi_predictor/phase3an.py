from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import SUPPORTED_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.repository import get_latest_crypto_features
from kalshi_predictor.crypto.semantics import DEFAULT_FEATURE_MAX_AGE_MINUTES
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.maintenance import sqlite_backup
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicMarketLink,
    Forecast,
    Market,
    MarketLeg,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    PaperPnl,
)
from kalshi_predictor.phase3an_crypto_source_quality import (
    build_phase3an_crypto_source_quality,
    source_quality_classification_for_phase3an,
    write_phase3an_crypto_source_quality_report,
)
from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AN_VERSION = "phase3an_v1"
PHASE_3AN_OPERATIONAL_VERSION = "phase3an_operational_gap_v1"
PHASE3AN_DASHBOARD_STATUS_FILENAME = "phase3an_dashboard_status.json"
DEFAULT_CRYPTO_SYMBOLS = ",".join(SUPPORTED_CRYPTO_SYMBOLS)

PHASE3AN_FUNNEL_STAGES = (
    "active_markets",
    "parsed_markets",
    "linked_direct_markets",
    "fresh_features",
    "valid_forecasts",
    "positive_raw_ev",
    "positive_executable_ev",
    "confidence_pass",
    "liquidity_pass",
    "spread_pass",
    "settlement_eligibility_pass",
    "phase3s_proceed",
    "phase3m_nonzero_size",
    "phase3n_approved",
    "paper_ready",
    "paper_order_created",
)

PHASE3AN_FUNNEL_REASON_CODES = (
    "NO_ACTIVE_MARKETS",
    "NO_DIRECT_MARKET",
    "NO_LINK_SAFE_MARKET",
    "NO_FRESH_FEATURES",
    "NO_VALID_FORECAST",
    "NO_POSITIVE_RAW_EV",
    "EV_LOST_TO_SPREAD",
    "EV_LOST_TO_COSTS",
    "LIQUIDITY_TOO_LOW",
    "SPREAD_TOO_WIDE",
    "CONFIDENCE_BELOW_THRESHOLD",
    "QUOTE_STALE",
    "SETTLEMENT_CHECK_FAILED",
    "PHASE_3S_SKIP",
    "PHASE_3M_ZERO_SIZE",
    "PHASE_3N_RISK_BLOCK",
    "MARKET_CLOSE_TOO_NEAR",
    "UNSUPPORTED_MARKET_TYPE",
    "DUPLICATE_IDEMPOTENCY_KEY",
    "UNKNOWN_REQUIRES_INVESTIGATION",
)

PHASE3AN_CRYPTO_CLASSIFICATIONS = {
    "HEALTHY",
    "RUNNING_CYCLE_OVERDUE",
    "WATCHER_STALE",
    "BLOCKED_BY_ACTIVE_WRITER",
    "BLOCKED_BY_MARKET_DATA_STALE",
    "BLOCKED_BY_EXPIRED_WINDOWS",
    "NO_DIRECT_MARKETS",
    "NO_POSITIVE_EV",
    "NO_EXECUTABLE_EV",
    "API_RATE_LIMIT_PRESSURE",
    "SOURCE_SERIES_EMPTY",
    "SOURCE_COVERAGE_GAP",
    "WAIT_FOR_MARKET_EV",
    "WAIT_FOR_EXECUTABLE_BOOK",
    "PAPER_READY_REVIEW",
    "RESTART_SAFE",
    "RESTART_NOT_SAFE",
    "UNKNOWN_REQUIRES_INVESTIGATION",
}


@dataclass(frozen=True)
class Phase3ANArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3ANJsonArtifactSet:
    output_dir: Path
    json_path: Path


@dataclass(frozen=True)
class Phase3ANGapFixArtifactSet:
    output_dir: Path
    summary_path: Path
    next_actions_path: Path
    manifest_path: Path
    artifact_paths: dict[str, Path]


def build_phase3an_crypto_feature_completeness(
    session: Session,
    *,
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    max_age_minutes: int = DEFAULT_FEATURE_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Report whether crypto_v2 has fresh point-in-time features for supported assets."""
    resolved = settings or get_settings()
    session.flush()
    now = utc_now()
    requested = [symbol.upper() for symbol in (symbols or list(SUPPORTED_CRYPTO_SYMBOLS))]
    rows = [
        _symbol_row(session, symbol=symbol, now=now, max_age_minutes=max_age_minutes)
        for symbol in requested
    ]
    missing = [row for row in rows if row["status"] == "MISSING"]
    stale = [row for row in rows if row["status"] == "STALE"]
    incomplete = [row for row in rows if row["status"] != "FRESH"]
    forecast_count = int(
        session.scalar(
            select(func.count())
            .select_from(Forecast)
            .where(Forecast.model_name == "crypto_v2")
        )
        or 0
    )
    return {
        "generated_at": now.isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_VERSION,
        "mode": "PAPER_ONLY_CRYPTO_FEATURE_COMPLETENESS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "settings": {
            "crypto_v2_min_link_confidence": str(resolved.crypto_v2_min_link_confidence),
            "crypto_v2_min_history_minutes": resolved.crypto_v2_min_history_minutes,
            "feature_max_age_minutes": max_age_minutes,
        },
        "summary": {
            "symbols_required": len(requested),
            "fresh_symbols": sum(1 for row in rows if row["status"] == "FRESH"),
            "missing_symbols": len(missing),
            "stale_symbols": len(stale),
            "linked_crypto_markets": _linked_market_count(session),
            "crypto_v2_forecasts": forecast_count,
            "can_rerun_crypto_v2": not incomplete,
        },
        "symbols": rows,
        "forecast_policy": {
            "block_missing_features": True,
            "block_stale_features": True,
            "block_future_or_post_settlement_features": True,
            "required_assets": requested,
        },
        "recommended_next_action": _next_action(incomplete),
        "next_commands": _next_commands(requested),
    }


def write_phase3an_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    symbols: list[str] | None = None,
    max_age_minutes: int = DEFAULT_FEATURE_MAX_AGE_MINUTES,
) -> Phase3ANArtifactSet:
    payload = build_phase3an_crypto_feature_completeness(
        session,
        settings=settings,
        symbols=symbols,
        max_age_minutes=max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3an_crypto_feature_completeness.json"
    markdown_path = output_dir / "phase3an_crypto_feature_completeness.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def _symbol_row(
    session: Session,
    *,
    symbol: str,
    now,
    max_age_minutes: int,
) -> dict[str, Any]:
    feature = get_latest_crypto_features(session, symbol)
    link_count = int(
        session.scalar(
            select(func.count())
            .select_from(CryptoMarketLink)
            .where(CryptoMarketLink.symbol == symbol)
        )
        or 0
    )
    if feature is None:
        return {
            "symbol": symbol,
            "status": "MISSING",
            "latest_feature_id": None,
            "latest_generated_at": None,
            "age_minutes": None,
            "link_count": link_count,
            "reason": "missing_crypto_feature",
        }
    generated_at = _aware(feature.generated_at)
    age = _aware(now) - generated_at
    age_minutes = max(0, int(age.total_seconds() // 60))
    status = "FRESH" if age <= timedelta(minutes=max_age_minutes) else "STALE"
    return {
        "symbol": symbol,
        "status": status,
        "latest_feature_id": feature.id,
        "latest_generated_at": generated_at.isoformat(),
        "age_minutes": age_minutes,
        "link_count": link_count,
        "reason": "fresh_point_in_time_feature"
        if status == "FRESH"
        else "stale_crypto_feature",
        "source": feature.source,
        "price": feature.price,
        "feature_version": _feature_version(feature),
        "quality_flags": _quality_flags(feature),
    }


def _feature_version(feature: CryptoFeature) -> str:
    raw = json.loads(feature.raw_json or "{}")
    return str(raw.get("feature_version") or raw.get("version") or "unknown")


def _quality_flags(feature: CryptoFeature) -> list[str]:
    flags: list[str] = []
    raw = json.loads(feature.raw_json or "{}")
    raw_flags = raw.get("quality_flags")
    if isinstance(raw_flags, list):
        flags.extend(str(item) for item in raw_flags)
    if not feature.price:
        flags.append("missing_price")
    return flags or ["ok"]


def _linked_market_count(session: Session) -> int:
    return int(
        session.scalar(select(func.count(func.distinct(CryptoMarketLink.ticker))))
        or 0
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _next_action(incomplete: list[dict[str, Any]]) -> str:
    if not incomplete:
        return "All required crypto assets have fresh features; rerun crypto_v2 forecasts."
    symbols = ",".join(row["symbol"] for row in incomplete)
    return f"Refresh crypto prices/features for {symbols}, then rerun crypto_v2."


def _next_commands(symbols: list[str]) -> list[str]:
    joined = ",".join(symbols)
    return [
        f"kalshi-bot ingest-crypto --symbols {joined} --source coinbase",
        f"kalshi-bot build-crypto-features --symbols {joined}",
        "kalshi-bot link-crypto-markets",
        "kalshi-bot forecast --model crypto_v2",
        "kalshi-bot phase3an-crypto-feature-completeness",
    ]


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AN Crypto Feature Completeness",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Symbol Readiness",
            "",
            "| Symbol | Status | Age minutes | Links | Reason |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in payload["symbols"]:
        age = row["age_minutes"] if row["age_minutes"] is not None else "n/a"
        lines.append(
            f"| {row['symbol']} | {row['status']} | {age} | "
            f"{row['link_count']} | {row['reason']} |"
        )
    lines.extend(["", "## Next Commands", "", "```bash"])
    lines.extend(payload["next_commands"])
    lines.extend(
        ["```", "", "## Recommended Next Action", "", payload["recommended_next_action"], ""]
    )
    return "\n".join(lines)


def build_phase3an_preflight(
    session: Session,
    *,
    settings: Settings | None = None,
    output_dir: Path = Path("reports/phase3an"),
) -> dict[str, Any]:
    """Runtime identity and fail-closed DB safety check for Phase 3AN."""
    from kalshi_predictor.phase3am import build_phase3am_runtime_identity

    runtime = build_phase3am_runtime_identity(session, settings=settings)
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-preflight",
        command_args={"output_dir": str(output_dir)},
    )
    return {
        **runtime,
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_OPERATIONAL_PREFLIGHT",
        "report_metadata": metadata,
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
    }


def write_phase3an_preflight_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_preflight(
        session,
        settings=settings,
        output_dir=output_dir,
    )
    json_path = output_dir / "runtime_identity.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_crypto_watch_doctor(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    freshness_minutes: int = 15,
) -> dict[str, Any]:
    """Explain the current crypto watch blocker without stopping or restarting it."""
    from kalshi_predictor.phase3ak import build_crypto_watch_status

    command_args = {
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "symbols": symbols,
        "freshness_minutes": freshness_minutes,
    }
    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    try:
        watch = build_crypto_watch_status(
            session,
            output_dir=reports_dir / "phase_3ak",
            settings=settings,
            symbols=symbols,
            freshness_minutes=freshness_minutes,
        )
        watch_error = None
    except Exception as exc:  # noqa: BLE001 - diagnostic must terminate with evidence.
        watch = _load_json(reports_dir / "phase_3ak" / "crypto_watch_status.json")
        watch_error = str(exc)

    if not watch:
        watch = _load_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-crypto-watch-doctor",
        command_args=command_args,
    )
    window_summary = _dict(watch.get("window_summary"))
    readiness = _dict(watch.get("readiness_funnel"))
    heartbeat = _dict(watch.get("runner_heartbeat") or watch.get("guard"))
    active_writer = _dict(watch.get("active_database_writer") or runtime.get("active_db_writer_status"))
    runner_state = str(
        watch.get("runner_state")
        or watch.get("watch_state")
        or _dict(watch.get("guard")).get("status")
        or "UNKNOWN"
    )
    runner_running = bool(watch.get("runner_running") or _dict(watch.get("guard")).get("running"))
    classification = _phase3an_crypto_classification(
        runner_state=runner_state,
        runner_running=runner_running,
        window_summary=window_summary,
        readiness=readiness,
        active_writer=active_writer,
    )
    source_quality = build_phase3an_crypto_source_quality(
        output_dir=output_dir,
        reports_dir=reports_dir,
        symbols=symbols,
        generated_at=metadata["generated_at"],
    )
    classification = _phase3an_classification_with_source_quality(
        classification,
        source_quality=source_quality,
    )
    slow_stage = _phase3an_crypto_slow_stage(watch, window_summary, readiness, runner_state)
    best_row = _best_ranking_row(session)
    restart_safe = classification == "RESTART_SAFE"
    payload = {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_CRYPTO_WATCH_DOCTOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "classification": classification,
        "watcher_state": watch.get("watch_state") or runner_state,
        "runner_state": runner_state,
        "runner_running": runner_running,
        "process_id": watch.get("runner_pid") or _dict(watch.get("guard")).get("pid"),
        "active_db_writer_identity": active_writer,
        "last_heartbeat": heartbeat.get("last_status_check") or heartbeat.get("heartbeat_at"),
        "last_completed_cycle": heartbeat.get("last_success") or heartbeat.get("latest_generated_at"),
        "current_in_flight_cycle_age_seconds": heartbeat.get("latest_age_seconds"),
        "expected_cadence_minutes": freshness_minutes,
        "overdue_threshold_seconds": heartbeat.get("freshness_seconds")
        or freshness_minutes * 60,
        "current_stage": slow_stage["current_stage"],
        "slowest_stage": slow_stage["slowest_stage"],
        "stage_evidence": slow_stage,
        "summary": {
            "markets_scanned": _intish(
                window_summary.get("rows_reviewed")
                or window_summary.get("markets_reviewed")
                or window_summary.get("active_crypto_markets")
            ),
            "crypto_markets_scanned": _intish(window_summary.get("active_crypto_markets")),
            "eligible_direct_markets": _intish(
                readiness.get("linked_markets")
                or readiness.get("active_windows")
                or window_summary.get("linked_markets")
            ),
            "expired_windows": _intish(window_summary.get("expired_windows")),
            "active_windows": _intish(window_summary.get("active_windows")),
            "fresh_quotes": _intish(
                readiness.get("fresh_quotes") or window_summary.get("fresh_quote_count")
            ),
            "stale_quotes": _intish(window_summary.get("stale_quote_count")),
            "fresh_forecasts": _intish(
                readiness.get("valid_forecasts") or window_summary.get("valid_forecasts")
            ),
            "positive_raw_ev_rows": _intish(
                readiness.get("positive_raw_ev") or window_summary.get("positive_raw_ev")
            ),
            "positive_executable_ev_rows": _intish(
                readiness.get("positive_executable_ev")
                or window_summary.get("positive_executable_ev")
            ),
            "paper_ready_rows": _intish(
                readiness.get("paper_ready_opportunities")
                or window_summary.get("paper_ready_opportunities")
            ),
            "restart_safe": restart_safe,
            "watch_build_error": watch_error,
            "source_quality_classification": source_quality_classification_for_phase3an(
                source_quality
            ),
            "source_missing_symbols": _dict(source_quality.get("summary")).get(
                "missing_symbols",
                [],
            ),
            "source_rate_limit_pressure": _dict(source_quality.get("summary")).get(
                "rate_limit_pressure",
            ),
        },
        "source_quality": source_quality,
        "best_row": best_row,
        "best_row_not_tradable_reason": _best_row_blocker(best_row),
        "restart_safe": restart_safe,
        "restart_policy": {
            "automatic_restart": False,
            "automatic_kill": False,
            "dry_run_plan_command": (
                "kalshi-bot phase3an-crypto-watch-restart-plan --dry-run "
                "--output-dir reports/phase3an"
            ),
        },
        "exact_next_action": _phase3an_crypto_next_action(classification, runner_state),
        "source_watch_status": watch,
    }
    if classification not in PHASE3AN_CRYPTO_CLASSIFICATIONS:
        payload["classification"] = "UNKNOWN_REQUIRES_INVESTIGATION"
    return payload


def write_phase3an_crypto_watch_doctor_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    freshness_minutes: int = 15,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    payload = build_phase3an_crypto_watch_doctor(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        symbols=symbols,
        freshness_minutes=freshness_minutes,
    )
    json_path = output_dir / "crypto_watch_doctor.json"
    _write_json(json_path, payload)
    write_phase3an_crypto_source_quality_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
        symbols=symbols,
    )
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_crypto_watch_restart_plan(
    session: Session,
    *,
    dry_run: bool = True,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
) -> dict[str, Any]:
    if not dry_run:
        raise ValueError("phase3an-crypto-watch-restart-plan only supports --dry-run.")
    doctor = build_phase3an_crypto_watch_doctor(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
    )
    metadata = doctor["report_metadata"]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_CRYPTO_WATCH_RESTART_PLAN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "dry_run": True,
        "would_stop_process": False,
        "would_start_process": False,
        "restart_safe": bool(doctor.get("restart_safe")),
        "watcher_state": doctor.get("classification"),
        "process_id": doctor.get("process_id"),
        "report_metadata": {
            **metadata,
            "command_arguments": {
                "command": "kalshi-bot phase3an-crypto-watch-restart-plan",
                "dry_run": True,
                "output_dir": str(output_dir),
                "reports_dir": str(reports_dir),
            },
        },
        "safety_flags": metadata["safety_flags"],
        "steps": _restart_plan_steps(doctor),
        "next_action": (
            "Do not restart automatically. If the watcher is stale and owned, run the "
            "explicit operator restart command after reviewing this dry-run plan."
        ),
    }


def write_phase3an_crypto_watch_restart_plan_report(
    session: Session,
    *,
    dry_run: bool = True,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    payload = build_phase3an_crypto_watch_restart_plan(
        session,
        dry_run=dry_run,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
    )
    json_path = output_dir / "crypto_watch_restart_plan.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_paper_funnel_explain(
    session: Session,
    *,
    window_hours: int = 168,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
) -> dict[str, Any]:
    from kalshi_predictor.phase3aj_gap_closure import build_paper_trade_funnel

    funnel = build_paper_trade_funnel(
        session,
        window_hours=window_hours,
        replay_readonly=True,
        settings=settings,
    )
    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    command_args = {"window_hours": window_hours, "output_dir": str(output_dir)}
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-paper-funnel-explain",
        command_args=command_args,
    )
    rows = [_normalize_funnel_row(row) for row in funnel.get("rows", [])]
    reason_counts = _normalized_reason_counts(funnel.get("reason_counts"), rows)
    stage_counts = _normalized_stage_counts(funnel.get("stage_counts"), reason_counts)
    first_hard_blocker = _first_hard_blocker(reason_counts)
    negative = _best_negative_ev_row(rows)
    positive_failed = [
        row for row in rows if row.get("raw_ev_positive") and not row.get("executable_ev_positive")
    ][:10]
    summary = _dict(funnel.get("summary"))
    payload = {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_PAPER_FUNNEL_EXPLAIN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "window_hours": window_hours,
        "thresholds": funnel.get("thresholds", {}),
        "summary": {
            "rankings_reviewed": _intish(summary.get("rankings_reviewed")),
            "tradeable_rows": _intish(
                summary.get("tradeable_paper_only") or summary.get("tradeable_rows")
            ),
            "paper_orders_created": 0,
            "paper_fills_created": 0,
            "first_hard_blocker": first_hard_blocker,
            "no_trade_correct_now": _intish(
                summary.get("tradeable_paper_only") or summary.get("tradeable_rows")
            )
            == 0,
            "status": summary.get("status") or "OK_EXPLAINED",
        },
        "stage_counts": stage_counts,
        "reason_counts": reason_counts,
        "top_block_reasons": sorted(
            reason_counts.items(), key=lambda item: item[1], reverse=True
        )[:10],
        "top_10_nearest_misses": [_normalize_funnel_row(row) for row in funnel.get("nearest_misses", [])][:10],
        "best_positive_raw_ev_rows_failed_execution": positive_failed,
        "best_negative_ev_row": negative,
        "best_negative_ev_reason": _best_row_blocker(negative),
        "per_model_breakdown": funnel.get("model_breakdown", {}),
        "per_category_breakdown": funnel.get("domain_breakdown", {}),
        "exact_next_action": funnel.get("next_action")
        or "No paper trade is expected from the current evidence.",
        "source_funnel": funnel,
    }
    return payload


def write_phase3an_paper_funnel_explain_report(
    session: Session,
    *,
    window_hours: int = 168,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_paper_funnel_explain(
        session,
        window_hours=window_hours,
        output_dir=output_dir,
        settings=settings,
    )
    json_path = output_dir / "paper_funnel_explain.json"
    markdown_path = output_dir / "paper_funnel_explain.md"
    _write_json(json_path, payload)
    markdown_path.write_text(_render_phase3an_paper_funnel_markdown(payload), encoding="utf-8")
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def build_phase3an_settlement_health_confirm(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    max_records: int = 5,
) -> dict[str, Any]:
    from kalshi_predictor.phase3am import build_phase3ay_due_settlement_diagnostic

    diagnostic = build_phase3ay_due_settlement_diagnostic(session, limit=max_records)
    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    command_args = {
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "max_records": max_records,
    }
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-settlement-health-confirm",
        command_args=command_args,
    )
    summary = _dict(diagnostic.get("summary"))
    open_orders = _open_paper_order_count(session)
    written_current_window = _recent_paper_pnl_count(session, hours=24)
    safe_to_apply = _intish(summary.get("safe_to_apply_count"))
    phase3ay_status = _load_json(reports_dir / "phase3ay" / "phase3ay_status.json")
    payload = {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_SETTLEMENT_HEALTH_CONFIRM",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "settlement_apply_ran": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "open_paper_trades": open_orders,
            "due_paper_trades": _intish(summary.get("due_paper_trades")),
            "overdue_paper_trades": _intish(summary.get("overdue_paper_trades")),
            "exact_eligible_trades": safe_to_apply,
            "already_settled_trades": _intish(summary.get("already_settled_trades")),
            "composite_local_trades": _intish(summary.get("composite_local_trades")),
            "sibling_ticker_candidates_rejected": _intish(
                summary.get("sibling_ticker_candidates_rejected")
            ),
            "ambiguous_candidates_rejected": _intish(
                summary.get("ambiguous_candidates_rejected")
            ),
            "written_settlements_current_window": written_current_window,
            "settlement_apply_needed": safe_to_apply > 0,
            "apply_command_exposed": safe_to_apply > 0,
            "status": "HEALTHY" if safe_to_apply == 0 else "EXACT_APPLY_AVAILABLE_REQUIRES_OPERATOR",
        },
        "phase3ay_watcher_status": phase3ay_status,
        "exact_apply_policy": {
            "phase3an_never_runs_apply": True,
            "requires_exact_only": True,
            "requires_apply_flag": True,
            "requires_backup_first": True,
            "requires_bounded_max_records": True,
            "requires_no_active_writer_conflict": True,
            "requires_dry_run_evidence": True,
            "operator_apply_command": None
            if safe_to_apply == 0
            else (
                "kalshi-bot phase3ay-settle-due-paper --exact-only --apply "
                "--backup-first --max-records 5"
            ),
        },
        "rows": diagnostic.get("rows", []),
        "exact_next_action": (
            "Settlement is healthy. Keep Phase 3AY exact-ticker watch running; "
            "do not settle sibling tickers."
            if safe_to_apply == 0
            else "Review dry-run evidence before any exact-only bounded settlement apply."
        ),
        "source_diagnostic": diagnostic,
    }
    return payload


def write_phase3an_settlement_health_confirm_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    max_records: int = 5,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    payload = build_phase3an_settlement_health_confirm(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        max_records=max_records,
    )
    json_path = output_dir / "settlement_health_confirm.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_3bb_r2_burndown(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    sources_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    settings: Settings | None = None,
    limit_per_bucket: int = 50,
) -> dict[str, Any]:
    from kalshi_predictor.phase3bb import (
        write_phase3bb_general_candidate_routing_report,
        write_phase3bb_general_source_availability_report,
        write_phase3bb_general_source_evidence_report,
        write_phase3bb_general_source_intake_report,
        write_phase3bb_group_source_review,
    )

    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    command_args = {
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "sources_dir": str(sources_dir),
        "evidence_dir": str(evidence_dir),
        "limit_per_bucket": limit_per_bucket,
        "write_evidence_files": False,
    }
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-3bb-r2-burndown",
        command_args=command_args,
    )
    candidate_artifacts = write_phase3bb_general_candidate_routing_report(
        session,
        output_dir=reports_dir / "phase3bb_r2",
        limit_per_bucket=limit_per_bucket,
    )
    intake_artifacts = write_phase3bb_general_source_intake_report(
        session,
        output_dir=sources_dir,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
        write_evidence_files=False,
    )
    evidence_artifacts = write_phase3bb_general_source_evidence_report(
        session,
        output_dir=sources_dir,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
    )
    availability_artifacts = write_phase3bb_general_source_availability_report(
        session,
        output_dir=sources_dir,
        evidence_dir=evidence_dir,
        limit_per_bucket=limit_per_bucket,
        check_source_urls=False,
    )
    intake = _load_json(intake_artifacts.json_path)
    evidence = _load_json(evidence_artifacts.json_path)
    availability = _load_json(availability_artifacts.json_path)
    group_status = _phase3an_group_source_review_status(
        template_csv=intake_artifacts.template_csv_path,
        sources_dir=sources_dir,
        writer=write_phase3bb_group_source_review,
    )
    general_sources = build_phase3an_general_sources_status(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        sources_dir=sources_dir,
        evidence_dir=evidence_dir,
        settings=settings,
    )
    intake_summary = _dict(intake.get("summary"))
    evidence_summary = _dict(evidence.get("summary"))
    availability_summary = _dict(availability.get("summary"))
    source_matrix = intake.get("source_readiness_matrix") if isinstance(intake.get("source_readiness_matrix"), list) else []
    payload = {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_3BB_R2_BURNDOWN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "source_template_rows": _intish(intake_summary.get("template_rows")),
            "commodity_group_count": _adapter_count(
                intake.get("template_rows"), "commodity_advertised_price_source"
            ),
            "infrastructure_group_count": _adapter_count(
                intake.get("template_rows"), "infrastructure_data_center_capacity_source"
            ),
            "transportation_group_count": _adapter_count(
                intake.get("template_rows"), "transportation_flight_cancellation_source"
            ),
            "evidence_ready_rows": _intish(
                evidence_summary.get("exact_evidence_ready_rows")
                or availability_summary.get("source_value_available_rows")
            ),
            "valid_input_rows": _intish(intake_summary.get("valid_input_rows")),
            "invalid_input_rows": _intish(intake_summary.get("invalid_input_rows")),
            "required_fields_missing": _missing_source_fields_count(intake.get("input_rows")),
            "usda_status": general_sources["sources"]["USDA"]["status"],
            "cushman_status": general_sources["sources"]["Cushman"]["status"],
            "flightaware_status": general_sources["sources"]["FlightAware"]["status"],
            "source_date_mismatch_blockers": general_sources["summary"]["source_date_mismatch_blockers"],
            "review_gated_rows": _review_gated_rows(source_matrix),
            "local_evidence_files_written": _intish(intake_summary.get("evidence_files_written")),
            "link_safe_rows": _safe_rows(source_matrix, "link_safe"),
            "forecast_safe_rows": _safe_rows(source_matrix, "forecast_safe"),
            "activation_ready_rows": 0,
            "db_writes": False,
            "link_writes": False,
            "feature_writes": False,
            "forecast_writes": False,
            "opportunity_writes": False,
            "paper_trade_writes": False,
            "settlement_writes": False,
        },
        "source_status": general_sources,
        "grouped_source_review_status": group_status,
        "artifact_sources": {
            "candidate_routing": str(candidate_artifacts.json_path),
            "source_intake": str(intake_artifacts.json_path),
            "source_evidence": str(evidence_artifacts.json_path),
            "source_availability": str(availability_artifacts.json_path),
        },
        "exact_next_action": _phase3an_source_next_action(general_sources),
        "source_intake_summary": intake_summary,
        "source_evidence_summary": evidence_summary,
        "source_availability_summary": availability_summary,
    }
    return payload


def write_phase3an_3bb_r2_burndown_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    sources_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    settings: Settings | None = None,
    limit_per_bucket: int = 50,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    sources_dir = _phase3an_usable_reports_path(sources_dir)
    payload = build_phase3an_3bb_r2_burndown(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        sources_dir=sources_dir,
        evidence_dir=evidence_dir,
        settings=settings,
        limit_per_bucket=limit_per_bucket,
    )
    json_path = output_dir / "3bb_r2_burndown.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_usda_date_mismatch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    expected_report_date: str = "July 3, 2026",
    settings: Settings | None = None,
) -> dict[str, Any]:
    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    command_args = {
        "output_dir": str(output_dir),
        "evidence_dir": str(evidence_dir),
        "expected_report_date": expected_report_date,
    }
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-usda-date-mismatch-report",
        command_args=command_args,
    )
    source_file = evidence_dir / "commodity_advertised_price_source.json"
    records = _records_from_json(source_file)
    usda_records = [
        record
        for record in records
        if "usda" in str(record.get("source_name") or record.get("source") or "").lower()
        or "usda" in str(record.get("source_url") or "").lower()
        or "avocado" in str(record.get("source_subject") or "").lower()
    ]
    local_dates = sorted(
        {
            str(record.get("as_of_date") or record.get("time_window") or "").strip()
            for record in usda_records
            if str(record.get("as_of_date") or record.get("time_window") or "").strip()
        }
    )
    exact_records = [
        record for record in usda_records if str(record.get("as_of_date") or "") == expected_report_date
    ]
    wrong_date_records = [
        record
        for record in usda_records
        if str(record.get("as_of_date") or "").strip()
        and str(record.get("as_of_date") or "").strip() != expected_report_date
    ]
    local_report_date = local_dates[0] if len(local_dates) == 1 else (local_dates or None)
    source_url = next(
        (str(record.get("source_url")) for record in usda_records if record.get("source_url")),
        None,
    )
    exact_exists = bool(exact_records)
    wrong_date = bool(wrong_date_records)
    if exact_exists:
        blocker = "USDA_VALUES_AVAILABLE_FOR_REVIEW"
    elif wrong_date:
        blocker = "USDA_DATE_MISMATCH"
    elif source_file.exists():
        blocker = "USDA_VALUES_UNAVAILABLE"
    else:
        blocker = "USDA_EXACT_REPORT_NOT_FOUND"
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_USDA_DATE_MISMATCH_REPORT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "target_market_tickers": _general_market_tickers(session, terms=("avocado", "hass", "usda")),
        "expected_report_date": expected_report_date,
        "local_report_date_found": local_report_date,
        "local_source_file_paths": [str(source_file)] if source_file.exists() else [],
        "source_url": source_url,
        "exact_expected_report_exists_locally": exact_exists,
        "archive_lookup_configured": False,
        "current_blocker": blocker,
        "uses_wrong_date_for_evidence": False,
        "wrong_date_records_reviewed": len(wrong_date_records),
        "next_action": (
            "Find exact official USDA July 3, 2026 evidence before filling observed_value."
            if blocker in {"USDA_DATE_MISMATCH", "USDA_EXACT_REPORT_NOT_FOUND"}
            else "Review exact July 3 USDA evidence before any source promotion."
        ),
    }


def write_phase3an_usda_date_mismatch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    expected_report_date: str = "July 3, 2026",
    settings: Settings | None = None,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_usda_date_mismatch_report(
        session,
        output_dir=output_dir,
        evidence_dir=evidence_dir,
        expected_report_date=expected_report_date,
        settings=settings,
    )
    json_path = output_dir / "usda_date_mismatch_report.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_general_sources_status(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    sources_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    settings: Settings | None = None,
) -> dict[str, Any]:
    runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    command_args = {
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "sources_dir": str(sources_dir),
        "evidence_dir": str(evidence_dir),
    }
    metadata = _phase3an_metadata_from_runtime(
        session,
        runtime,
        command="kalshi-bot phase3an-general-sources-status",
        command_args=command_args,
    )
    usda = build_phase3an_usda_date_mismatch_report(
        session,
        output_dir=output_dir,
        evidence_dir=evidence_dir,
        settings=settings,
    )
    evidence = _load_json(sources_dir / "phase3bb_r2_general_source_evidence.json")
    availability = _load_json(sources_dir / "phase3bb_r2_general_source_availability.json")
    activation = _load_json(
        reports_dir / "phase3bb_r3_source_activation" / "source_evidence_activation.json"
    )
    flightaware_gate = _load_json(
        reports_dir / "phase3bb_r4_flightaware" / "flightaware_review_link_gate.json"
    )
    flightaware_date_stable = _load_json(
        reports_dir / "phase3bb_r5_flightaware" / "flightaware_date_stable_evidence.json"
    )
    evidence_summary = _dict(evidence.get("summary"))
    availability_summary = _dict(availability.get("summary"))
    source_evidence_ready = _intish(
        evidence_summary.get("exact_evidence_ready_rows")
        or availability_summary.get("source_value_available_rows")
    )
    source_gate = _phase3an_general_source_gate_summary(
        usda=usda,
        source_evidence_ready=source_evidence_ready,
        activation=activation,
        flightaware_gate=flightaware_gate,
        flightaware_date_stable=flightaware_date_stable,
    )
    sources = source_gate["sources"]
    payload = {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_GENERAL_SOURCES_STATUS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "source_evidence_ready_rows": source_evidence_ready,
            "source_evidence_status": source_gate["source_evidence_status"],
            "activation_readiness": source_gate["activation_readiness"],
            "first_hard_blocker": source_gate["first_hard_blocker"],
            "official_free_source_rows": source_gate["official_free_source_rows"],
            "date_stable_rows": source_gate["date_stable_rows"],
            "date_stable_missing_rows": source_gate["date_stable_missing_rows"],
            "review_gated_rows": source_gate["review_gated_rows"],
            "blocked_rows": source_gate["blocked_rows"],
            "proprietary_blocked_rows": source_gate["proprietary_blocked_rows"],
            "wrong_date_rows": source_gate["wrong_date_rows"],
            "source_date_mismatch_blockers": 1
            if source_gate["source_date_mismatch_blockers"]
            else 0,
            "proprietary_review_blockers": source_gate["proprietary_review_blockers"],
            "review_required_blockers": source_gate["review_required_blockers"],
            "link_safe_rows": source_gate["link_safe_rows"],
            "forecast_safe_rows": source_gate["forecast_safe_rows"],
            "activation_candidate_rows": source_gate["activation_candidate_rows"],
            "promoted_to_link_safe_rows": 0,
            "promoted_to_forecast_safe_rows": 0,
            "safe_to_create_links": source_gate["link_safe_rows"] > 0,
            "safe_to_create_forecasts": source_gate["forecast_safe_rows"] > 0,
            "safe_to_create_paper_trades": False,
            "phase3ax_r5_source_activation_complete": source_gate[
                "phase3ax_r5_source_activation_complete"
            ],
        },
        "sources": sources,
        "source_activation_decisions": source_gate["source_activation_decisions"],
        "source_reports": {
            "usda_date_mismatch": str(output_dir / "usda_date_mismatch_report.json"),
            "source_evidence": str(sources_dir / "phase3bb_r2_general_source_evidence.json"),
            "source_availability": str(sources_dir / "phase3bb_r2_general_source_availability.json"),
            "phase3bb_r3_source_activation": str(
                reports_dir / "phase3bb_r3_source_activation" / "source_evidence_activation.json"
            ),
            "phase3bb_r4_flightaware": str(
                reports_dir / "phase3bb_r4_flightaware" / "flightaware_review_link_gate.json"
            ),
            "phase3bb_r5_flightaware": str(
                reports_dir
                / "phase3bb_r5_flightaware"
                / "flightaware_date_stable_evidence.json"
            ),
        },
        "exact_next_action": source_gate["next_action"],
    }
    return payload


def write_phase3an_general_sources_status_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    sources_dir: Path = Path("reports/phase3bb_r2_sources"),
    evidence_dir: Path = Path("data/general_source_evidence"),
    settings: Settings | None = None,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    sources_dir = _phase3an_usable_reports_path(sources_dir)
    payload = build_phase3an_general_sources_status(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        sources_dir=sources_dir,
        evidence_dir=evidence_dir,
        settings=settings,
    )
    json_path = output_dir / "general_sources_status.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_sports_blocker_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> dict[str, Any]:
    from kalshi_predictor.phase3am import build_phase3am_sports_gap_watch

    sports = build_phase3am_sports_gap_watch(reports_dir=reports_dir)
    coverage = _load_json(reports_dir / "market_coverage" / "market_coverage_doctor.json")
    summary = _dict(sports.get("summary"))
    reason_codes = _sports_reason_codes(summary, sports.get("reason_codes"))
    metadata = _phase3an_file_report_metadata(
        command="kalshi-bot phase3an-sports-blocker-report",
        command_args={"output_dir": str(output_dir), "reports_dir": str(reports_dir)},
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_SPORTS_BLOCKER_REPORT",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "feature_writes": False,
        "forecast_writes": False,
        "opportunity_writes": False,
        "paper_trade_writes": False,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": metadata["command_arguments"],
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "degraded_market_coverage_rows": _intish(
                _dict(coverage.get("summary")).get("degraded_rows")
                or _dict(coverage.get("summary")).get("blocked_rows")
            ),
            "partial_provenance_markets": _intish(summary.get("partial_provenance_sports_markets")),
            "partial_link_rows": _intish(summary.get("partial_provenance_sports_markets")),
            "unlinked_parsed_sports_markets": _intish(summary.get("unlinked_parsed_sports_markets")),
            "placeholder_rows": _intish(summary.get("placeholder_rows")),
            "world_cup_round_placeholders": _intish(summary.get("unresolved_round_placeholders")),
            "schedule_evidence_available": bool(summary.get("schedule_evidence_available")),
            "roster_team_evidence_available": bool(summary.get("roster_team_evidence_available")),
            "safe_repair_rows": _intish(summary.get("safe_repair_rows")),
            "blocked_rows": _intish(summary.get("blocked_rows")),
        },
        "reason_codes": reason_codes,
        "exact_next_action": sports.get("recommended_next_action")
        or "Keep sports placeholders and partial provenance blocked until evidence exists.",
        "source_sports_report": sports,
    }


def write_phase3an_sports_blocker_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    payload = build_phase3an_sports_blocker_report(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    json_path = output_dir / "sports_blocker_report.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def _phase3an_cached_domain_readiness(output_dir: Path) -> tuple[dict[str, Any] | None, Path]:
    reports_dir = output_dir.parent if output_dir.name == "phase3an" else output_dir
    readiness_path = reports_dir / "phase3bb" / "phase3bb_domain_readiness.json"
    payload = _load_json(readiness_path)
    return (payload or None, readiness_path)


def build_phase3an_economic_news_watch(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    handoff_limit: int = 25,
    rebuild_readiness: bool = False,
    include_preflight: bool = False,
) -> dict[str, Any]:
    from kalshi_predictor.phase3am import build_economic_news_market_watch

    output_dir = _phase3an_usable_reports_path(output_dir)
    cached_readiness: dict[str, Any] | None = None
    cached_readiness_path: Path | None = None
    readiness_source = "live_rebuild" if rebuild_readiness else "cache_miss_minimal"
    if not rebuild_readiness:
        cached_readiness, cached_readiness_path = _phase3an_cached_domain_readiness(output_dir)
        if cached_readiness:
            readiness_source = "cached_phase3bb_domain_readiness"
    economic = build_economic_news_market_watch(
        session,
        handoff_limit=handoff_limit,
        readiness=cached_readiness,
        rebuild_readiness=rebuild_readiness,
    )
    command_args = {
        "output_dir": str(output_dir),
        "handoff_limit": handoff_limit,
        "rebuild_readiness": rebuild_readiness,
        "include_preflight": include_preflight,
        "cached_readiness_path": str(cached_readiness_path) if cached_readiness_path else None,
    }
    if include_preflight:
        runtime = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
        metadata = _phase3an_metadata_from_runtime(
            session,
            runtime,
            command="kalshi-bot phase3an-economic-news-watch",
            command_args=command_args,
        )
        preflight_source = "live_phase3an_preflight"
    else:
        metadata = _phase3an_file_report_metadata(
            command="kalshi-bot phase3an-economic-news-watch",
            command_args=command_args,
        )
        preflight_source = "skipped_bounded_report_only"
    summary = _dict(economic.get("summary"))
    domains = _dict(economic.get("domains"))
    current_handoff = _dict(economic.get("current_market_handoff"))
    economic_handoff = _dict(_dict(current_handoff.get("domains")).get("economic"))
    news_handoff = _dict(_dict(current_handoff.get("domains")).get("news"))
    economic_counts = _dict(economic_handoff.get("counts"))
    news_counts = _dict(news_handoff.get("counts"))
    first_hard_blocker = _economic_news_watch_first_hard_blocker(
        economic_handoff,
        news_handoff,
    )
    exact_next_action = _economic_news_watch_next_action(first_hard_blocker)
    source_freshness = _economic_news_source_freshness(
        context_ready_count=_intish(summary.get("context_ready_count")),
        domains=domains,
        readiness_source=readiness_source,
    )
    return {
        "generated_at": metadata["generated_at"],
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_ECONOMIC_NEWS_WATCH",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "links_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "readiness_source": readiness_source,
        "readiness_path": str(cached_readiness_path) if cached_readiness_path else None,
        "preflight_source": preflight_source,
        "report_metadata": metadata,
        "git_commit": metadata["git_commit"],
        "database_fingerprint": metadata["database_fingerprint"],
        "command_arguments": command_args,
        "data_watermark": metadata["data_watermark"],
        "safety_flags": metadata["safety_flags"],
        "summary": {
            "economic_compatible_parsed_markets": _intish(
                summary.get("economic_compatible_active_markets")
            ),
            "economic_status": _domain_status(_dict(domains.get("economic"))),
            "news_compatible_parsed_markets": _intish(
                summary.get("news_compatible_active_markets")
            ),
            "news_status": _domain_status(_dict(domains.get("news"))),
            "economic_current_parsed_markets": _intish(
                summary.get("economic_current_parsed_markets")
            ),
            "news_current_parsed_markets": _intish(summary.get("news_current_parsed_markets")),
            "economic_exact_linked_current_markets": _intish(
                summary.get("economic_exact_linked_current_markets")
            ),
            "news_exact_linked_current_markets": _intish(
                summary.get("news_exact_linked_current_markets")
            ),
            "economic_exact_linked_current_without_parsed_leg": _intish(
                summary.get("economic_exact_linked_current_without_parsed_leg")
            ),
            "news_exact_linked_current_without_parsed_leg": _intish(
                summary.get("news_exact_linked_current_without_parsed_leg")
            ),
            "economic_current_parsed_missing_exact_link": _intish(
                economic_counts.get("current_parsed_missing_exact_link")
            ),
            "news_current_parsed_missing_exact_link": _intish(
                news_counts.get("current_parsed_missing_exact_link")
            ),
            "exact_linked_current_markets": _intish(
                summary.get("economic_exact_linked_current_markets")
            )
            + _intish(summary.get("news_exact_linked_current_markets")),
            "exact_linked_current_without_parsed_leg": _intish(
                summary.get("economic_exact_linked_current_without_parsed_leg")
            )
            + _intish(summary.get("news_exact_linked_current_without_parsed_leg")),
            "current_parsed_missing_exact_link": _intish(
                economic_counts.get("current_parsed_missing_exact_link")
            )
            + _intish(news_counts.get("current_parsed_missing_exact_link")),
            "economic_current_handoff_blocker": summary.get(
                "economic_current_handoff_blocker"
            ),
            "news_current_handoff_blocker": summary.get("news_current_handoff_blocker"),
            "context_ready_count": _intish(summary.get("context_ready_count")),
            "active_market_count": _intish(summary.get("parsed_market_count")),
            "readiness_source": readiness_source,
            "preflight_source": preflight_source,
            "source_freshness": source_freshness,
            "first_hard_blocker": first_hard_blocker,
            "compatibility_status": _economic_news_watch_status(first_hard_blocker),
            "next_refresh": economic.get("next_refresh_time"),
            "blocker_reason": first_hard_blocker,
            "domain_readiness_blocker_reason": economic.get("blocked_reason")
            or "WAITING_FOR_COMPATIBLE_MARKETS",
            "next_registered_command": _economic_news_watch_next_command(
                first_hard_blocker
            ),
        },
        "domains": domains,
        "current_market_handoff": current_handoff,
        "exact_next_action": exact_next_action,
        "source_watch": economic,
    }


def write_phase3an_economic_news_watch_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    handoff_limit: int = 25,
    rebuild_readiness: bool = False,
    include_preflight: bool = False,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_news_watch(
        session,
        output_dir=output_dir,
        settings=settings,
        handoff_limit=handoff_limit,
        rebuild_readiness=rebuild_readiness,
        include_preflight=include_preflight,
    )
    json_path = output_dir / "economic_news_watch.json"
    _write_json(json_path, payload)
    (output_dir / "ECONOMIC_NEWS_WATCH.md").write_text(
        _render_phase3an_economic_news_watch_markdown(payload),
        encoding="utf-8",
    )
    (output_dir / "NEXT_ACTIONS.md").write_text(
        _render_phase3an_economic_news_next_actions(payload),
        encoding="utf-8",
    )
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def _economic_news_watch_first_hard_blocker(
    economic_handoff: dict[str, Any],
    news_handoff: dict[str, Any],
) -> str:
    blockers = {
        str(economic_handoff.get("first_blocker") or ""),
        str(news_handoff.get("first_blocker") or ""),
    }
    for blocker in (
        "READY_FOR_FORECASTS",
        "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
        "EXACT_LINKS_MISSING",
        "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS",
        "NO_CURRENT_PARSED_MARKETS",
        "WAITING_FOR_COMPATIBLE_MARKETS",
    ):
        if blocker in blockers:
            return blocker
    return "WAITING_FOR_COMPATIBLE_MARKETS"


def _economic_news_watch_status(first_hard_blocker: str) -> str:
    if first_hard_blocker == "READY_FOR_FORECASTS":
        return "CURRENT_COMPATIBLE_MARKETS_READY_FOR_FORECASTS"
    if first_hard_blocker == "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL":
        return "PARSER_BACKFILL_REQUIRED"
    if first_hard_blocker == "EXACT_LINKS_MISSING":
        return "EXACT_LINKS_MISSING"
    if first_hard_blocker == "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS":
        return "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS"
    if first_hard_blocker == "NO_CURRENT_PARSED_MARKETS":
        return "NO_CURRENT_PARSED_MARKETS"
    return "WAITING_FOR_COMPATIBLE_MARKETS"


def _economic_news_source_freshness(
    *,
    context_ready_count: int,
    domains: dict[str, Any],
    readiness_source: str,
) -> str:
    if context_ready_count <= 0:
        return "NO_CONTEXT_READY"
    if readiness_source == "cached_phase3bb_domain_readiness":
        return "CONTEXT_READY_FROM_CACHED_READINESS"
    if readiness_source == "live_rebuild":
        return "CONTEXT_READY_FROM_LIVE_REBUILD"
    return "CONTEXT_READY"


def _economic_news_watch_next_command(first_hard_blocker: str) -> str:
    if first_hard_blocker == "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL":
        return (
            "kalshi-bot phase3an-economic-news-parser-backfill-plan "
            "--output-dir reports/phase3an --limit 500"
        )
    if first_hard_blocker == "READY_FOR_FORECASTS":
        return "kalshi-bot phase3an-economic-news-watch --output-dir reports/phase3an"
    if first_hard_blocker == "EXACT_LINKS_MISSING":
        return (
            "kalshi-bot phase3an-economic-news-watch "
            "--output-dir reports/phase3an --handoff-limit 500"
        )
    return "kalshi-bot phase3an-economic-news-watch --output-dir reports/phase3an"


def _economic_news_watch_next_action(first_hard_blocker: str) -> str:
    if first_hard_blocker == "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL":
        return (
            "Run the registered report-only parser backfill plan to classify exact "
            "current links before any operator-approved DB repair."
        )
    if first_hard_blocker == "READY_FOR_FORECASTS":
        return (
            "Current exact-linked parsed economic/news markets exist; keep forecast "
            "and paper gates read-only until existing forecast diagnostics approve."
        )
    if first_hard_blocker == "EXACT_LINKS_MISSING":
        return (
            "Current parsed markets need exact ticker links; do not use fuzzy or "
            "sibling matching."
        )
    if first_hard_blocker == "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS":
        return "Parsed economic/news markets are historical only; keep watching current markets."
    return (
        "No compatible current parsed economic/news market exists yet; keep market "
        "refresh and source watches running."
    )


def _render_phase3an_economic_news_watch_markdown(payload: dict[str, Any]) -> str:
    summary = _dict(payload.get("summary"))
    return "\n".join(
        [
            "# Phase 3AN Economic/News Watch",
            "",
            f"- Generated at: `{payload.get('generated_at')}`",
            "- Mode: `PAPER_READ_ONLY_ECONOMIC_NEWS_WATCH`",
            f"- Compatibility status: `{summary.get('compatibility_status')}`",
            f"- First hard blocker: `{summary.get('first_hard_blocker')}`",
            (
                "- Economic current parsed markets: "
                f"`{summary.get('economic_current_parsed_markets', 0)}`"
            ),
            (
                "- Economic exact-linked current without parsed leg: "
                f"`{summary.get('economic_exact_linked_current_without_parsed_leg', 0)}`"
            ),
            (
                "- News current parsed markets: "
                f"`{summary.get('news_current_parsed_markets', 0)}`"
            ),
            f"- Context-ready count: `{summary.get('context_ready_count', 0)}`",
            f"- Source freshness: `{summary.get('source_freshness')}`",
            "",
            f"Next action: {payload.get('exact_next_action')}",
            "",
            "No links, forecasts, opportunities, paper trades, or exchange writes are created.",
            "",
        ]
    )


def _render_phase3an_economic_news_next_actions(payload: dict[str, Any]) -> str:
    summary = _dict(payload.get("summary"))
    command = str(
        summary.get("next_registered_command")
        or "kalshi-bot phase3an-economic-news-watch --output-dir reports/phase3an"
    )
    return "\n".join(
        [
            "# Phase 3AN Economic/News Next Actions",
            "",
            f"Current blocker: `{summary.get('first_hard_blocker')}`",
            "",
            "Registered command only:",
            "",
            "```bash",
            command,
            "```",
            "",
            "Do not force links, forecasts, opportunities, paper trades, or exchange writes.",
            "",
        ]
    )


def build_phase3an_economic_news_parser_backfill_plan(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    watch = build_phase3an_economic_news_watch(
        session,
        output_dir=output_dir,
        settings=settings,
        handoff_limit=limit,
    )
    handoff = _dict(watch.get("current_market_handoff"))
    domains = _dict(handoff.get("domains"))
    rows: list[dict[str, Any]] = []
    for domain, domain_payload in domains.items():
        link_only_rows = _dict(domain_payload).get("link_only_rows")
        if not isinstance(link_only_rows, list):
            continue
        for row in link_only_rows:
            if not isinstance(row, dict):
                continue
            assessment = _assess_economic_news_parser_backfill_row(
                session,
                domain,
                row,
            )
            rows.append(
                {
                    "domain": domain,
                    "ticker": row.get("ticker"),
                    "title": row.get("title"),
                    "event_ticker": row.get("event_ticker"),
                    "series_ticker": row.get("series_ticker"),
                    "close_time": row.get("close_time"),
                    "link_reference": row.get("link_reference"),
                    "reason_codes": row.get("reason_codes"),
                    **assessment,
                }
            )
    reason_counts = _parser_backfill_reason_counts(rows)
    safe_to_backfill_now = sum(
        1 for row in rows if row.get("safe_to_backfill_parser_leg") is True
    )
    summary = _dict(watch.get("summary"))
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_ECONOMIC_NEWS_PARSER_BACKFILL_PLAN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "links_created": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "parser_rows_written": 0,
        "command_arguments": {
            "output_dir": str(output_dir),
            "limit": limit,
        },
        "summary": {
            "rows_reviewed": len(rows),
            "economic_exact_linked_current_without_parsed_leg": _intish(
                summary.get("economic_exact_linked_current_without_parsed_leg")
            ),
            "news_exact_linked_current_without_parsed_leg": _intish(
                summary.get("news_exact_linked_current_without_parsed_leg")
            ),
            "safe_to_backfill_now": safe_to_backfill_now,
            "unsafe_to_backfill_now": max(0, len(rows) - safe_to_backfill_now),
            "parser_backfill_reason_counts": reason_counts,
            "first_blocker": _parser_backfill_first_blocker(rows),
        },
        "rows": rows,
        "source_watch": watch,
        "exact_next_action": _parser_backfill_next_action(rows),
    }


def write_phase3an_economic_news_parser_backfill_plan_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    limit: int = 500,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_news_parser_backfill_plan(
        session,
        output_dir=output_dir,
        settings=settings,
        limit=limit,
    )
    json_path = output_dir / "economic_news_parser_backfill_plan.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_economic_link_event_repair_plan(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    parser_plan = build_phase3an_economic_news_parser_backfill_plan(
        session,
        output_dir=output_dir,
        settings=settings,
        limit=limit,
    )
    rows = [
        _economic_link_event_repair_row(row)
        for row in parser_plan.get("rows", [])
        if isinstance(row, dict) and row.get("domain") == "economic"
    ]
    link_repair_candidates = [
        row for row in rows if row["safe_to_repair_link_event"] is True
    ]
    safe_parser_rows = [
        row for row in rows if row["safe_to_backfill_parser_leg"] is True
    ]
    after_repair_safe = [
        row
        for row in rows
        if row["safe_to_backfill_parser_leg_after_link_repair"] is True
    ]
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R2",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_ECONOMIC_LINK_EVENT_REPAIR_PLAN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "link_rows_written": 0,
        "parser_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "operator_gated_parser_backfill": True,
        "apply_command_exposed": False,
        "command_arguments": {
            "output_dir": str(output_dir),
            "limit": limit,
        },
        "summary": {
            "rows_reviewed": len(rows),
            "event_mismatch_rows": _count_rows(rows, "unsafe_reason", "LINK_PARSER_EVENT_MISMATCH"),
            "safe_parser_backfill_rows": len(safe_parser_rows),
            "link_event_repair_candidates": len(link_repair_candidates),
            "parser_backfill_allowed_after_link_repair": len(after_repair_safe),
            "link_event_repair_blocked_rows": max(0, len(rows) - len(link_repair_candidates)),
            "current_event_key_counts": _count_values_simple(rows, "current_event_key"),
            "normalized_link_event_counts": _count_values_simple(rows, "normalized_link_event"),
            "parser_event_counts": _count_values_simple(rows, "parser_event"),
            "suggested_event_key_counts": _count_values_simple(rows, "suggested_event_key"),
            "first_blocker": _economic_link_event_first_blocker(rows),
        },
        "rows": rows,
        "source_parser_backfill_plan": parser_plan,
        "exact_next_action": _economic_link_event_next_action(rows),
    }


def write_phase3an_economic_link_event_repair_plan_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    limit: int = 500,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_link_event_repair_plan(
        session,
        output_dir=output_dir,
        settings=settings,
        limit=limit,
    )
    json_path = output_dir / "economic_link_event_repair_plan.json"
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_economic_link_event_repair_apply(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 50,
    limit: int = 500,
) -> dict[str, Any]:
    if max_records <= 0:
        raise ValueError("--max-records must be positive.")
    if dry_run and apply:
        raise ValueError("Use either --dry-run or --apply, not both.")
    if apply and not backup_first:
        raise ValueError("--apply requires --backup-first.")

    resolved = settings or get_settings()
    db_url = _phase3an_session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    repair_plan = build_phase3an_economic_link_event_repair_plan(
        session,
        output_dir=output_dir,
        settings=resolved,
        limit=limit,
    )
    candidates = [
        row for row in repair_plan["rows"] if row.get("safe_to_repair_link_event") is True
    ][:max_records]
    status = "DRY_RUN"
    blocked_reason = None
    backup_path = None
    written_rows: list[dict[str, Any]] = []

    if apply and not bool(writer.get("safe_to_start_write", True)):
        status = "BLOCKED_BY_ACTIVE_WRITER"
        blocked_reason = "Another DB writer owns the database."
    elif apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        backup_path = sqlite_backup(
            output_path=output_dir
            / "backups"
            / f"phase3an_economic_link_event_repair_{_timestamp_for_path()}.db",
            db_url=db_url,
        )
        now = utc_now()
        for row in candidates:
            latest_link = _latest_economic_link(session, str(row["ticker"]))
            if latest_link is None:
                continue
            repaired = EconomicMarketLink(
                ticker=str(row["ticker"]),
                event_key=str(row["suggested_event_key"]),
                detected_at=now,
                category=latest_link.category,
                confidence=latest_link.confidence,
                reason=(
                    "phase3an_r3_exact_link_event_repair:"
                    f" {row['current_event_key']} -> {row['suggested_event_key']}"
                ),
                raw_json=encode_json(
                    {
                        "source": "phase3an_economic_link_event_repair_apply",
                        "previous_link_id": latest_link.id,
                        "previous_event_key": row["current_event_key"],
                        "suggested_event_key": row["suggested_event_key"],
                        "parser_event": row["parser_event"],
                        "candidate_parser_leg": row.get("candidate_parser_leg"),
                    }
                ),
            )
            session.add(repaired)
            written_rows.append(
                {
                    "ticker": row["ticker"],
                    "previous_event_key": row["current_event_key"],
                    "new_event_key": row["suggested_event_key"],
                    "parser_event": row["parser_event"],
                }
            )
        session.flush()
        session.commit()
        status = "APPLIED" if written_rows else "NO_REPAIRABLE_ROWS"

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R3",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "ECONOMIC_LINK_EVENT_REPAIR_APPLY" if apply else "ECONOMIC_LINK_EVENT_REPAIR_DRY_RUN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "parser_rows_written": 0,
        "link_rows_written": len(written_rows),
        "dry_run": dry_run,
        "apply": apply,
        "backup_first": backup_first,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "status": status,
        "blocked_reason": blocked_reason,
        "max_records": max_records,
        "active_db_writer_status": writer,
        "summary": {
            "repair_candidates_reviewed": len(candidates),
            "would_write_link_rows": len(candidates) if not apply else 0,
            "link_rows_written": len(written_rows),
            "parser_rows_written": 0,
            "safe_parser_backfill_after_link_repair": sum(
                1
                for row in candidates
                if row.get("safe_to_backfill_parser_leg_after_link_repair") is True
            ),
            "status": status,
            "first_blocker": _economic_link_event_apply_first_blocker(
                apply=apply,
                status=status,
                candidates=candidates,
            ),
        },
        "candidate_rows": candidates,
        "written_rows": written_rows,
        "source_repair_plan": repair_plan,
        "exact_next_action": _economic_link_event_apply_next_action(
            apply=apply,
            status=status,
            candidates=candidates,
        ),
    }


def write_phase3an_economic_link_event_repair_apply_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 50,
    limit: int = 500,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_link_event_repair_apply(
        session,
        output_dir=output_dir,
        settings=settings,
        dry_run=dry_run,
        apply=apply,
        backup_first=backup_first,
        max_records=max_records,
        limit=limit,
    )
    json_path = output_dir / (
        "economic_link_event_repair_apply.json"
        if apply
        else "economic_link_event_repair_dry_run.json"
    )
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_economic_parser_leg_backfill(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 50,
    limit: int = 500,
) -> dict[str, Any]:
    if max_records <= 0:
        raise ValueError("--max-records must be positive.")
    if dry_run and apply:
        raise ValueError("Use either --dry-run or --apply, not both.")
    if apply and not backup_first:
        raise ValueError("--apply requires --backup-first.")

    resolved = settings or get_settings()
    db_url = _phase3an_session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    parser_plan = build_phase3an_economic_news_parser_backfill_plan(
        session,
        output_dir=output_dir,
        settings=resolved,
        limit=limit,
    )
    rows = [
        _economic_parser_leg_backfill_row(row)
        for row in parser_plan.get("rows", [])
        if isinstance(row, dict) and row.get("domain") == "economic"
    ]
    candidates = [
        row for row in rows if row.get("safe_to_write_parser_leg") is True
    ][:max_records]
    blocked_reason_counts = _parser_leg_backfill_blocked_reason_counts(rows)
    status = "DRY_RUN"
    blocked_reason = None
    backup_path = None
    written_rows: list[dict[str, Any]] = []

    if apply and not candidates:
        status = "NO_SAFE_PARSER_BACKFILL_ROWS"
    elif apply and not bool(writer.get("safe_to_start_write", True)):
        status = "BLOCKED_BY_ACTIVE_WRITER"
        blocked_reason = "Another DB writer owns the database."
    elif apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        backup_path = sqlite_backup(
            output_path=output_dir
            / "backups"
            / f"phase3an_economic_parser_leg_backfill_{_timestamp_for_path()}.db",
            db_url=db_url,
        )
        now = utc_now()
        for row in candidates:
            candidate = _dict(row.get("candidate_parser_leg"))
            leg = MarketLeg(
                ticker=str(row["ticker"]),
                leg_index=int(candidate["leg_index"]),
                parsed_at=now,
                side=str(candidate["side"]),
                category=str(candidate["category"]),
                market_type=str(candidate["market_type"]),
                entity_name=_optional_str(candidate.get("entity_name")),
                operator=str(candidate["operator"]),
                threshold_value=_optional_str(candidate.get("threshold_value")),
                unit=_optional_str(candidate.get("unit")),
                confidence=str(candidate["confidence"]),
                raw_text=str(candidate["raw_text"]),
                reason=f"phase3an_r4_exact_link_parser_backfill: {candidate['reason']}",
                raw_json=encode_json(
                    {
                        "source": "phase3an_economic_parser_leg_backfill",
                        "candidate_parser_leg": candidate,
                        "link_reference": row.get("link_reference"),
                        "parser_event": row.get("parser_event"),
                        "current_event_key": row.get("current_event_key"),
                        "safety_policy": "operator_approved_local_parser_leg_only",
                    }
                ),
            )
            session.add(leg)
            written_rows.append(
                {
                    "ticker": row["ticker"],
                    "leg_index": candidate["leg_index"],
                    "parser_event": row.get("parser_event"),
                    "current_event_key": row.get("current_event_key"),
                }
            )
        session.flush()
        session.commit()
        status = "APPLIED" if written_rows else "NO_SAFE_PARSER_BACKFILL_ROWS"

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R4",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "ECONOMIC_PARSER_LEG_BACKFILL_APPLY"
        if apply
        else "ECONOMIC_PARSER_LEG_BACKFILL_DRY_RUN",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "link_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "parser_rows_written": len(written_rows),
        "dry_run": dry_run,
        "apply": apply,
        "backup_first": backup_first,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "status": status,
        "blocked_reason": blocked_reason,
        "max_records": max_records,
        "active_db_writer_status": writer,
        "summary": {
            "rows_reviewed": len(rows),
            "safe_parser_backfill_rows": sum(
                1 for row in rows if row.get("safe_to_write_parser_leg") is True
            ),
            "blocked_parser_backfill_rows": sum(
                1 for row in rows if row.get("safe_to_write_parser_leg") is not True
            ),
            "blocked_reason_counts": blocked_reason_counts,
            "candidate_rows_reviewed": len(candidates),
            "would_write_parser_rows": len(candidates) if not apply else 0,
            "parser_rows_written": len(written_rows),
            "link_rows_written": 0,
            "features_created": 0,
            "forecasts_created": 0,
            "opportunities_created": 0,
            "paper_trades_created": 0,
            "status": status,
            "first_blocker": _economic_parser_leg_backfill_first_blocker(
                apply=apply,
                status=status,
                candidates=candidates,
                blocked_reason_counts=blocked_reason_counts,
            ),
        },
        "candidate_rows": candidates,
        "blocked_rows": [
            row for row in rows if row.get("safe_to_write_parser_leg") is not True
        ],
        "written_rows": written_rows,
        "source_parser_backfill_plan": parser_plan,
        "exact_next_action": _economic_parser_leg_backfill_next_action(
            apply=apply,
            status=status,
            candidates=candidates,
            blocked_reason_counts=blocked_reason_counts,
        ),
    }


def write_phase3an_economic_parser_leg_backfill_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    dry_run: bool = True,
    apply: bool = False,
    backup_first: bool = False,
    max_records: int = 50,
    limit: int = 500,
) -> Phase3ANJsonArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_parser_leg_backfill(
        session,
        output_dir=output_dir,
        settings=settings,
        dry_run=dry_run,
        apply=apply,
        backup_first=backup_first,
        max_records=max_records,
        limit=limit,
    )
    json_path = output_dir / (
        "economic_parser_leg_backfill_apply.json"
        if apply
        else "economic_parser_leg_backfill_dry_run.json"
    )
    _write_json(json_path, payload)
    return Phase3ANJsonArtifactSet(output_dir, json_path)


def build_phase3an_economic_operator_approval_packet(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    max_records: int = 50,
    limit: int = 500,
) -> dict[str, Any]:
    if max_records <= 0:
        raise ValueError("--max-records must be positive.")

    resolved = settings or get_settings()
    db_url = _phase3an_session_db_url(session) or database_url_from_settings(resolved)
    writer = db_writer_monitor(settings=resolved, db_url=db_url)
    link_dry_run = build_phase3an_economic_link_event_repair_apply(
        session,
        output_dir=output_dir,
        settings=resolved,
        dry_run=True,
        apply=False,
        backup_first=False,
        max_records=max_records,
        limit=limit,
    )
    parser_dry_run = build_phase3an_economic_parser_leg_backfill(
        session,
        output_dir=output_dir,
        settings=resolved,
        dry_run=True,
        apply=False,
        backup_first=False,
        max_records=max_records,
        limit=limit,
    )
    link_candidates = list(link_dry_run.get("candidate_rows") or [])
    parser_candidates = list(parser_dry_run.get("candidate_rows") or [])
    parser_blocked = list(parser_dry_run.get("blocked_rows") or [])
    blocked_reason_counts = _dict(_dict(parser_dry_run.get("summary")).get("blocked_reason_counts"))
    status = _economic_operator_approval_packet_status(
        link_candidates=link_candidates,
        parser_candidates=parser_candidates,
        writer=writer,
    )
    first_blocker = _economic_operator_approval_packet_first_blocker(
        status=status,
        link_candidates=link_candidates,
        parser_candidates=parser_candidates,
        blocked_reason_counts=blocked_reason_counts,
    )
    command_sequence = _economic_operator_approval_command_sequence(
        output_dir=output_dir,
        limit=limit,
        max_records=max_records,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R5",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_ECONOMIC_OPERATOR_APPROVAL_PACKET",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "link_rows_written": 0,
        "parser_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "operator_approval_required": bool(link_candidates or parser_candidates),
        "auto_apply_supported": False,
        "status": status,
        "active_db_writer_status": writer,
        "summary": {
            "link_repair_candidates": len(link_candidates),
            "parser_backfill_candidates": len(parser_candidates),
            "parser_blocked_rows": len(parser_blocked),
            "parser_blocked_reason_counts": blocked_reason_counts,
            "link_rows_written": 0,
            "parser_rows_written": 0,
            "paper_trades_created": 0,
            "db_writer_safe_to_start": bool(writer.get("safe_to_start_write", True)),
            "first_blocker": first_blocker,
        },
        "review_checklist": [
            "Confirm each link-event repair row is an exact ticker match.",
            "Confirm parser event and corrected link event agree.",
            "Confirm no news rows, fuzzy matches, sibling tickers, or expired rows are included.",
            "Run db-writer-monitor immediately before any later apply.",
            "Use --backup-first for every local DB apply command.",
        ],
        "registered_operator_command_sequence": command_sequence,
        "candidate_ticker_sets": {
            "link_repair": _candidate_tickers(link_candidates),
            "parser_backfill": _candidate_tickers(parser_candidates),
            "parser_blocked_sample": _candidate_tickers(parser_blocked[:20]),
        },
        "source_dry_runs": {
            "link_event_repair": _compact_link_event_dry_run(link_dry_run),
            "parser_leg_backfill": _compact_parser_leg_dry_run(parser_dry_run),
        },
        "exact_next_action": _economic_operator_approval_next_action(
            status=status,
            link_candidates=link_candidates,
            parser_candidates=parser_candidates,
        ),
    }


def write_phase3an_economic_operator_approval_packet_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    max_records: int = 50,
    limit: int = 500,
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_operator_approval_packet(
        session,
        output_dir=output_dir,
        settings=settings,
        max_records=max_records,
        limit=limit,
    )
    json_path = output_dir / "economic_operator_approval_packet.json"
    markdown_path = output_dir / "ECONOMIC_OPERATOR_APPROVAL_PACKET.md"
    _write_json(json_path, payload)
    markdown_path.write_text(
        _render_phase3an_economic_operator_approval_packet_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def build_phase3an_economic_approval_safety_guard(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    max_records: int = 50,
    limit: int = 500,
) -> dict[str, Any]:
    packet = build_phase3an_economic_operator_approval_packet(
        session,
        output_dir=output_dir,
        settings=settings,
        max_records=max_records,
        limit=limit,
    )
    return build_phase3an_economic_approval_safety_guard_from_packet(packet)


def build_phase3an_economic_approval_safety_guard_from_packet(
    packet: dict[str, Any],
) -> dict[str, Any]:
    command_sequence = list(packet.get("registered_operator_command_sequence") or [])
    command_audit = _audit_economic_operator_commands(command_sequence)
    source_dry_runs = _dict(packet.get("source_dry_runs"))
    source_write_audit = _economic_packet_source_write_audit(source_dry_runs)
    guard_failures = [
        *command_audit["failures"],
        *source_write_audit["failures"],
    ]
    if packet.get("auto_apply_supported") is not False:
        guard_failures.append("AUTO_APPLY_SUPPORTED_MUST_BE_FALSE")
    if packet.get("live_or_demo_execution") is not False:
        guard_failures.append("LIVE_OR_DEMO_EXECUTION_MUST_BE_FALSE")
    if packet.get("paper_trade_creation") is not False:
        guard_failures.append("PAPER_TRADE_CREATION_MUST_BE_FALSE")

    guard_status = "PASS_REPORT_ONLY" if not guard_failures else "FAIL_REVIEW_REQUIRED"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R6",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_ECONOMIC_APPROVAL_SAFETY_GUARD",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "link_rows_written": 0,
        "parser_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "guard_status": guard_status,
        "guard_failures": guard_failures,
        "summary": {
            "approval_packet_status": packet.get("status"),
            "operator_approval_required": packet.get("operator_approval_required"),
            "auto_apply_supported": packet.get("auto_apply_supported"),
            "commands_reviewed": len(command_sequence),
            "unregistered_commands": command_audit["unregistered_commands"],
            "unguarded_apply_commands": command_audit["unguarded_apply_commands"],
            "apply_commands_missing_backup_first": command_audit[
                "apply_commands_missing_backup_first"
            ],
            "source_write_failures": source_write_audit["failures"],
            "guard_status": guard_status,
            "first_blocker": "NONE" if guard_status == "PASS_REPORT_ONLY" else guard_failures[0],
        },
        "command_audit": command_audit,
        "source_write_audit": source_write_audit,
        "source_approval_packet": packet,
        "exact_next_action": (
            "Approval packet command sequence is report-only guarded; wait for human "
            "approval before any later --apply --backup-first command."
            if guard_status == "PASS_REPORT_ONLY"
            else "Review economic_approval_safety_guard.json before any apply command."
        ),
    }


def write_phase3an_economic_approval_safety_guard_from_packet_report(
    *,
    packet_path: Path,
    output_dir: Path = Path("reports/phase3an"),
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    packet = _load_json(packet_path)
    if not packet:
        raise ValueError(f"Approval packet not found or unreadable: {packet_path}")
    payload = build_phase3an_economic_approval_safety_guard_from_packet(packet)
    payload["source_packet_path"] = str(packet_path)
    json_path = output_dir / "economic_approval_safety_guard.json"
    markdown_path = output_dir / "ECONOMIC_APPROVAL_SAFETY_GUARD.md"
    _write_json(json_path, payload)
    markdown_path.write_text(
        _render_phase3an_economic_approval_safety_guard_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def write_phase3an_economic_approval_safety_guard_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    settings: Settings | None = None,
    max_records: int = 50,
    limit: int = 500,
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_approval_safety_guard(
        session,
        output_dir=output_dir,
        settings=settings,
        max_records=max_records,
        limit=limit,
    )
    json_path = output_dir / "economic_approval_safety_guard.json"
    markdown_path = output_dir / "ECONOMIC_APPROVAL_SAFETY_GUARD.md"
    _write_json(json_path, payload)
    markdown_path.write_text(
        _render_phase3an_economic_approval_safety_guard_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def build_phase3an_economic_morning_operator_handoff(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> dict[str, Any]:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    packet = _load_json(output_dir / "economic_operator_approval_packet.json")
    guard = _load_json(output_dir / "economic_approval_safety_guard.json")
    r5_status = _load_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    health = _load_json(reports_dir / "phase3ay" / "phase3ay_health_refresh.json")

    packet_summary = _dict(packet.get("summary"))
    r5_guard = _dict(r5_status.get("guard"))
    r5_latest = _dict(r5_status.get("latest_summary"))
    market_health = _dict(health.get("market_health"))
    paper_health = _dict(health.get("paper_health"))
    status = _morning_handoff_status(packet=packet, guard=guard, r5_status=r5_status)
    hard_stop_reasons = _morning_handoff_hard_stops(packet=packet, guard=guard)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R7",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_MORNING_OPERATOR_HANDOFF",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "link_rows_written": 0,
        "parser_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "status": status,
        "hard_stop_reasons": hard_stop_reasons,
        "summary": {
            "approval_packet_status": packet.get("status"),
            "approval_guard_status": guard.get("guard_status"),
            "operator_approval_required": packet.get("operator_approval_required"),
            "link_repair_candidates": packet_summary.get("link_repair_candidates"),
            "parser_backfill_candidates": packet_summary.get("parser_backfill_candidates"),
            "parser_blocked_rows": packet_summary.get("parser_blocked_rows"),
            "guard_failures": guard.get("guard_failures") or [],
            "r5_status": r5_guard.get("status"),
            "r5_running": r5_guard.get("running"),
            "r5_pid": r5_guard.get("pid"),
            "r5_cycle_number": r5_latest.get("cycle_number"),
            "r5_total_cycles": r5_latest.get("total_cycles"),
            "positive_ev_rows": r5_latest.get("positive_ev_rows"),
            "paper_ready_candidates": r5_latest.get("paper_ready_candidates"),
            "market_refresh_status": market_health.get("status"),
            "markets_seen": market_health.get("markets_seen"),
            "snapshots_inserted": market_health.get("snapshots_inserted"),
            "forecasts_inserted": market_health.get("forecasts_inserted"),
            "paper_health_status": paper_health.get("status"),
            "eligible_exact_settlements": paper_health.get("eligible_exact_settlements"),
            "paper_pnl_realized": paper_health.get("paper_pnl_realized"),
            "first_blocker": hard_stop_reasons[0] if hard_stop_reasons else "AWAITING_OPERATOR_REVIEW",
        },
        "review_order": [
            "Read ECONOMIC_OPERATOR_APPROVAL_PACKET.md.",
            "Read ECONOMIC_APPROVAL_SAFETY_GUARD.md.",
            "Confirm R5 remains paper-only and no paper-ready candidates exist before applying anything.",
            "Run db-writer-monitor immediately before any approved --apply --backup-first command.",
            "Apply link repair first only if approved, rerun parser plan, then apply parser backfill only if approved.",
        ],
        "registered_next_commands": [
            "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
            (
                "kalshi-bot phase3an-economic-approval-safety-guard "
                "--output-dir reports/phase3an "
                "--packet-path reports/phase3an/economic_operator_approval_packet.json"
            ),
            "kalshi-bot db-writer-monitor",
            (
                "AFTER HUMAN APPROVAL ONLY: kalshi-bot "
                "phase3an-economic-link-event-repair --output-dir reports/phase3an "
                "--limit 500 --max-records 50 --apply --backup-first"
            ),
            (
                "AFTER HUMAN APPROVAL ONLY: kalshi-bot "
                "phase3an-economic-parser-leg-backfill --output-dir reports/phase3an "
                "--limit 500 --max-records 50 --apply --backup-first"
            ),
        ],
        "source_artifacts": {
            "approval_packet": str(output_dir / "economic_operator_approval_packet.json"),
            "approval_safety_guard": str(output_dir / "economic_approval_safety_guard.json"),
            "r5_status": str(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"),
            "health_refresh": str(reports_dir / "phase3ay" / "phase3ay_health_refresh.json"),
        },
        "exact_next_action": _morning_handoff_next_action(
            status=status,
            hard_stop_reasons=hard_stop_reasons,
        ),
    }


def write_phase3an_economic_morning_operator_handoff_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_economic_morning_operator_handoff(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    json_path = output_dir / "economic_morning_operator_handoff.json"
    markdown_path = output_dir / "ECONOMIC_MORNING_OPERATOR_HANDOFF.md"
    _write_json(json_path, payload)
    markdown_path.write_text(
        _render_phase3an_economic_morning_operator_handoff_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def build_phase3an_overnight_refresh_continuity(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> dict[str, Any]:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    r5_status = _load_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    health = _load_json(reports_dir / "phase3ay" / "phase3ay_health_refresh.json")
    handoff = _load_json(output_dir / "economic_morning_operator_handoff.json")
    guard = _load_json(output_dir / "economic_approval_safety_guard.json")

    r5_guard = _dict(r5_status.get("guard"))
    r5_latest = _dict(r5_status.get("latest_summary"))
    market_health = _dict(health.get("market_health"))
    paper_health = _dict(health.get("paper_health"))
    handoff_summary = _dict(handoff.get("summary"))
    continuity_flags = _overnight_continuity_flags(
        r5_status=r5_status,
        health=health,
        handoff=handoff,
        guard=guard,
    )
    status = "CONTINUE_SAFE_REFRESH" if not continuity_flags else "REVIEW_BEFORE_REFRESH"
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN-R8",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "REPORT_ONLY_OVERNIGHT_REFRESH_CONTINUITY",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "link_rows_written": 0,
        "parser_rows_written": 0,
        "features_created": 0,
        "forecasts_created": 0,
        "opportunities_created": 0,
        "paper_trades_created": 0,
        "status": status,
        "continuity_flags": continuity_flags,
        "summary": {
            "r5_status": r5_guard.get("status"),
            "r5_running": r5_guard.get("running"),
            "r5_pid": r5_guard.get("pid"),
            "r5_cycle_number": r5_latest.get("cycle_number"),
            "r5_total_cycles": r5_latest.get("total_cycles"),
            "positive_ev_rows": r5_latest.get("positive_ev_rows"),
            "paper_ready_candidates": r5_latest.get("paper_ready_candidates"),
            "market_refresh_mode": health.get("mode"),
            "market_refresh_status": market_health.get("status"),
            "markets_seen": market_health.get("markets_seen"),
            "snapshots_inserted": market_health.get("snapshots_inserted"),
            "forecasts_inserted": market_health.get("forecasts_inserted"),
            "paper_health_status": paper_health.get("status"),
            "eligible_exact_settlements": paper_health.get("eligible_exact_settlements"),
            "paper_pnl_realized": paper_health.get("paper_pnl_realized"),
            "handoff_status": handoff.get("status"),
            "handoff_first_blocker": handoff_summary.get("first_blocker"),
            "approval_guard_status": guard.get("guard_status"),
            "first_blocker": continuity_flags[0] if continuity_flags else "NONE",
        },
        "safe_overnight_commands": [
            "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
            (
                "kalshi-bot phase3ay-health-refresh --output-dir reports/phase3ay "
                "--cycles 1 --interval-seconds 0 --settlement-only "
                "--settlement-limit 100 --settlement-max-pages 2"
            ),
            (
                "kalshi-bot phase3an-economic-morning-operator-handoff "
                "--output-dir reports/phase3an --reports-dir reports"
            ),
            (
                "kalshi-bot phase3an-overnight-refresh-continuity "
                "--output-dir reports/phase3an --reports-dir reports"
            ),
        ],
        "blocked_without_human_approval": [
            "kalshi-bot phase3an-economic-link-event-repair --apply --backup-first",
            "kalshi-bot phase3an-economic-parser-leg-backfill --apply --backup-first",
        ],
        "source_artifacts": {
            "r5_status": str(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json"),
            "health_refresh": str(reports_dir / "phase3ay" / "phase3ay_health_refresh.json"),
            "morning_handoff": str(output_dir / "economic_morning_operator_handoff.json"),
            "approval_safety_guard": str(output_dir / "economic_approval_safety_guard.json"),
        },
        "exact_next_action": (
            "Continue bounded status and settlement-only refresh commands while waiting "
            "for human approval; do not run apply commands."
            if status == "CONTINUE_SAFE_REFRESH"
            else "Review continuity flags before running another refresh command."
        ),
    }


def write_phase3an_overnight_refresh_continuity_report(
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
) -> Phase3ANArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    payload = build_phase3an_overnight_refresh_continuity(
        output_dir=output_dir,
        reports_dir=reports_dir,
    )
    json_path = output_dir / "overnight_refresh_continuity.json"
    markdown_path = output_dir / "OVERNIGHT_REFRESH_CONTINUITY.md"
    _write_json(json_path, payload)
    markdown_path.write_text(
        _render_phase3an_overnight_refresh_continuity_markdown(payload),
        encoding="utf-8",
    )
    return Phase3ANArtifactSet(output_dir, json_path, markdown_path)


def write_phase3an_gap_fix_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3an"),
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    window_hours: int = 168,
    max_settlements: int = 5,
    limit_per_bucket: int = 50,
) -> Phase3ANGapFixArtifactSet:
    output_dir = _phase3an_usable_reports_path(output_dir)
    reports_dir = _phase3an_usable_reports_path(reports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths: dict[str, Path] = {}

    preflight = build_phase3an_preflight(session, settings=settings, output_dir=output_dir)
    artifact_paths["runtime_identity"] = output_dir / "runtime_identity.json"
    _write_json(artifact_paths["runtime_identity"], preflight)

    crypto = build_phase3an_crypto_watch_doctor(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
    )
    artifact_paths["crypto_watch_doctor"] = output_dir / "crypto_watch_doctor.json"
    _write_json(artifact_paths["crypto_watch_doctor"], crypto)

    paper = build_phase3an_paper_funnel_explain(
        session,
        window_hours=window_hours,
        output_dir=output_dir,
        settings=settings,
    )
    artifact_paths["paper_funnel_explain"] = output_dir / "paper_funnel_explain.json"
    artifact_paths["paper_funnel_explain_md"] = output_dir / "paper_funnel_explain.md"
    _write_json(artifact_paths["paper_funnel_explain"], paper)
    artifact_paths["paper_funnel_explain_md"].write_text(
        _render_phase3an_paper_funnel_markdown(paper),
        encoding="utf-8",
    )

    settlement = build_phase3an_settlement_health_confirm(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        max_records=max_settlements,
    )
    artifact_paths["settlement_health_confirm"] = output_dir / "settlement_health_confirm.json"
    _write_json(artifact_paths["settlement_health_confirm"], settlement)

    burndown = build_phase3an_3bb_r2_burndown(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        sources_dir=reports_dir / "phase3bb_r2_sources",
        evidence_dir=Path("data/general_source_evidence"),
        settings=settings,
        limit_per_bucket=limit_per_bucket,
    )
    artifact_paths["3bb_r2_burndown"] = output_dir / "3bb_r2_burndown.json"
    _write_json(artifact_paths["3bb_r2_burndown"], burndown)

    usda = build_phase3an_usda_date_mismatch_report(
        session,
        output_dir=output_dir,
        evidence_dir=Path("data/general_source_evidence"),
        settings=settings,
    )
    artifact_paths["usda_date_mismatch_report"] = output_dir / "usda_date_mismatch_report.json"
    _write_json(artifact_paths["usda_date_mismatch_report"], usda)

    general = build_phase3an_general_sources_status(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        sources_dir=reports_dir / "phase3bb_r2_sources",
        evidence_dir=Path("data/general_source_evidence"),
        settings=settings,
    )
    artifact_paths["general_sources_status"] = output_dir / "general_sources_status.json"
    _write_json(artifact_paths["general_sources_status"], general)

    sports = build_phase3an_sports_blocker_report(output_dir=output_dir, reports_dir=reports_dir)
    artifact_paths["sports_blocker_report"] = output_dir / "sports_blocker_report.json"
    _write_json(artifact_paths["sports_blocker_report"], sports)

    economic = build_phase3an_economic_news_watch(
        session,
        output_dir=output_dir,
        settings=settings,
    )
    artifact_paths["economic_news_watch"] = output_dir / "economic_news_watch.json"
    _write_json(artifact_paths["economic_news_watch"], economic)

    phase3az = _phase3az_before_after(reports_dir=reports_dir)
    artifact_paths["phase3az_before_after"] = output_dir / "phase3az_before_after.json"
    _write_json(artifact_paths["phase3az_before_after"], phase3az)

    dashboard = _phase3an_dashboard_status(
        crypto=crypto,
        paper=paper,
        settlement=settlement,
        burndown=burndown,
        general=general,
        sports=sports,
        economic=economic,
    )
    artifact_paths["dashboard_status"] = output_dir / PHASE3AN_DASHBOARD_STATUS_FILENAME
    _write_json(artifact_paths["dashboard_status"], dashboard)

    summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    summary_path.write_text(
        _render_phase3an_executive_summary(
            crypto=crypto,
            paper=paper,
            settlement=settlement,
            burndown=burndown,
            general=general,
            sports=sports,
            economic=economic,
            phase3az=phase3az,
        ),
        encoding="utf-8",
    )
    next_actions_path.write_text(
        _render_phase3an_next_actions(
            crypto=crypto,
            general=general,
            sports=sports,
            economic=economic,
        ),
        encoding="utf-8",
    )
    artifact_paths["executive_summary"] = summary_path
    artifact_paths["next_actions"] = next_actions_path

    manifest_path = output_dir / "MANIFEST.sha256"
    _write_manifest(manifest_path, list(artifact_paths.values()))
    return Phase3ANGapFixArtifactSet(output_dir, summary_path, next_actions_path, manifest_path, artifact_paths)


def _phase3an_metadata_from_runtime(
    session: Session,
    runtime: dict[str, Any],
    *,
    command: str,
    command_args: dict[str, Any],
) -> dict[str, Any]:
    args = {"command": command, **command_args}
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "repository_root": runtime.get("repository_root"),
        "git_branch": runtime.get("git_branch"),
        "git_commit": runtime.get("git_commit"),
        "git_dirty": runtime.get("git_dirty"),
        "python_executable": runtime.get("python_executable") or str(Path(sys.executable).resolve()),
        "installed_package_path": runtime.get("installed_package_path"),
        "resolved_database_url": runtime.get("resolved_database_url"),
        "database_fingerprint": runtime.get("database_fingerprint"),
        "migration_revision": runtime.get("migration_revision"),
        "timezone": runtime.get("timezone"),
        "command_arguments": args,
        "data_watermark": _phase3an_data_watermark(session),
        "safety_flags": _phase3an_safety_flags(),
        "active_db_writer_status": runtime.get("active_db_writer_status"),
        "ui_database_identity": runtime.get("ui_database_identity"),
        "cli_database_identity": runtime.get("cli_database_identity"),
        "worker_database_identity": runtime.get("worker_database_identity"),
    }


def _phase3an_file_report_metadata(
    *,
    command: str,
    command_args: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "repository_root": str(Path.cwd().resolve()),
        "git_branch": "UNKNOWN_GIT_BRANCH",
        "git_commit": "UNKNOWN_GIT_COMMIT",
        "git_dirty": "UNKNOWN",
        "python_executable": str(Path(sys.executable).resolve()),
        "installed_package_path": str(Path(__file__).resolve()),
        "resolved_database_url": "UNKNOWN_DATABASE_URL",
        "database_fingerprint": "UNKNOWN_DATABASE_FINGERPRINT",
        "migration_revision": "UNKNOWN_MIGRATION_REVISION",
        "timezone": "unknown",
        "command_arguments": {"command": command, **command_args},
        "data_watermark": {"file_report_only": True},
        "safety_flags": _phase3an_safety_flags(),
    }


def _phase3an_safety_flags() -> dict[str, Any]:
    return {
        "paper_only": True,
        "read_only_by_default": True,
        "live_trading_enabled": False,
        "demo_exchange_writes_enabled": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "creates_paper_orders": False,
        "creates_features": False,
        "creates_forecasts": False,
        "creates_opportunities": False,
        "settlement_apply_ran": False,
        "allows_sibling_settlement": False,
        "allows_fuzzy_settlement": False,
        "thresholds_lowered": False,
    }


def _phase3an_data_watermark(session: Session) -> dict[str, Any]:
    return {
        "latest_market_seen_at": _latest_iso(session, Market.last_seen_at),
        "latest_snapshot_at": _latest_iso(session, MarketSnapshot.captured_at),
        "latest_forecast_at": _latest_iso(session, Forecast.forecasted_at),
        "latest_ranking_at": _latest_iso(session, MarketRanking.ranked_at),
        "latest_paper_order_at": _latest_iso(session, PaperOrder.created_at),
        "latest_paper_pnl_at": _latest_iso(session, PaperPnl.calculated_at),
    }


def _latest_iso(session: Session, column: Any) -> str | None:
    try:
        value = session.scalar(select(func.max(column)))
    except Exception:  # noqa: BLE001 - metadata best effort only.
        return None
    return value.isoformat() if hasattr(value, "isoformat") else None


def _write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _phase3an_usable_reports_path(path: Path) -> Path:
    resolved = Path(path)
    if resolved.is_absolute() or not resolved.parts or resolved.parts[0] != "reports":
        return resolved
    reports_root = Path("reports")
    if reports_root.is_symlink() and not reports_root.exists():
        return Path.cwd().parent.joinpath(*resolved.parts)
    return resolved


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _intish(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _phase3an_crypto_classification(
    *,
    runner_state: str,
    runner_running: bool,
    window_summary: dict[str, Any],
    readiness: dict[str, Any],
    active_writer: dict[str, Any],
) -> str:
    writer_active = bool(
        active_writer.get("active_writer")
        or active_writer.get("current_writer_pid")
        or active_writer.get("pid")
    ) and not bool(active_writer.get("safe_to_start_write", True))
    if writer_active:
        return "BLOCKED_BY_ACTIVE_WRITER"
    if runner_state == "RUNNING_CYCLE_OVERDUE":
        return "RUNNING_CYCLE_OVERDUE"
    if runner_state in {"RUNNER_STALE", "WATCHER_STALE"}:
        return "WATCHER_STALE"
    if not runner_running and runner_state in {"STOPPED", "STOPPED_WITH_STALE_PID", "NO_UNATTENDED_JOB"}:
        return "RESTART_SAFE"
    active_windows = _intish(window_summary.get("active_windows"))
    expired_windows = _intish(window_summary.get("expired_windows"))
    fresh_quotes = _intish(readiness.get("fresh_quotes") or window_summary.get("fresh_quote_count"))
    stale_quotes = _intish(window_summary.get("stale_quote_count"))
    positive_raw = _intish(readiness.get("positive_raw_ev") or window_summary.get("positive_raw_ev"))
    positive_executable = _intish(
        readiness.get("positive_executable_ev") or window_summary.get("positive_executable_ev")
    )
    paper_ready = _intish(
        readiness.get("paper_ready_opportunities") or window_summary.get("paper_ready_opportunities")
    )
    linked = _intish(readiness.get("linked_markets") or window_summary.get("linked_markets"))
    if active_windows == 0 and expired_windows > 0:
        return "BLOCKED_BY_EXPIRED_WINDOWS"
    if linked == 0:
        return "NO_DIRECT_MARKETS"
    if stale_quotes > 0 or fresh_quotes == 0:
        return "BLOCKED_BY_MARKET_DATA_STALE"
    if positive_raw == 0:
        return "NO_POSITIVE_EV"
    if positive_executable == 0:
        return "NO_EXECUTABLE_EV"
    if paper_ready > 0:
        return "HEALTHY"
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _phase3an_crypto_slow_stage(
    watch: dict[str, Any],
    window_summary: dict[str, Any],
    readiness: dict[str, Any],
    runner_state: str,
) -> dict[str, Any]:
    watch_summary = _dict(watch.get("watch_summary"))
    stage = str(
        watch_summary.get("current_stage")
        or watch_summary.get("stage")
        or watch.get("current_stage")
        or runner_state
        or "UNKNOWN"
    )
    candidates = {
        "window_sync": _intish(window_summary.get("stale_windows")) + _intish(window_summary.get("expired_windows")),
        "market_quotes": _intish(window_summary.get("stale_quote_count")),
        "forecasts": max(
            _intish(readiness.get("active_windows")) - _intish(readiness.get("valid_forecasts")),
            0,
        ),
        "positive_ev": max(
            _intish(readiness.get("valid_forecasts")) - _intish(readiness.get("positive_raw_ev")),
            0,
        ),
        "execution": max(
            _intish(readiness.get("positive_raw_ev")) - _intish(readiness.get("positive_executable_ev")),
            0,
        ),
    }
    slowest = max(candidates.items(), key=lambda item: item[1])[0] if candidates else stage
    if runner_state == "RUNNING_CYCLE_OVERDUE" and not candidates.get(slowest):
        slowest = "heartbeat_or_cycle_completion"
    return {
        "current_stage": stage,
        "slowest_stage": slowest,
        "stage_counts": candidates,
    }


def _best_ranking_row(session: Session) -> dict[str, Any] | None:
    try:
        row = session.scalars(
            select(MarketRanking).order_by(desc(MarketRanking.ranked_at), desc(MarketRanking.opportunity_score)).limit(1)
        ).first()
    except Exception:  # noqa: BLE001 - report missing best row rather than failing.
        return None
    if row is None:
        return None
    return {
        "ticker": row.ticker,
        "title": row.title,
        "ranked_at": row.ranked_at.isoformat() if row.ranked_at else None,
        "forecast_model": row.forecast_model,
        "estimated_edge": row.estimated_edge,
        "opportunity_score": row.opportunity_score,
        "best_price": row.best_price,
        "best_side": row.best_side,
        "reason": row.reason,
    }


def _best_row_blocker(row: dict[str, Any] | None) -> str:
    if not row:
        return "NO_ROW_AVAILABLE"
    reason = str(row.get("reason_code") or row.get("reason") or "")
    if reason:
        return reason
    edge = row.get("estimated_edge") or row.get("raw_ev") or row.get("executable_ev")
    try:
        if float(edge) <= 0:
            return "NO_POSITIVE_RAW_EV"
    except (TypeError, ValueError):
        pass
    return "UNKNOWN_REQUIRES_INVESTIGATION"


def _phase3an_crypto_next_action(classification: str, runner_state: str) -> str:
    if classification == "RUNNING_CYCLE_OVERDUE":
        return "Inspect slow-stage evidence; run the dry-run restart plan only if heartbeat remains stale."
    if classification == "RESTART_SAFE":
        return "Review dry-run restart steps; no automatic process stop was performed."
    if classification == "API_RATE_LIMIT_PRESSURE":
        return "Keep the watch paper-only and reduce source pressure before any acceleration."
    if classification == "SOURCE_SERIES_EMPTY":
        return "Verify the zero-market source series before rerunning the same watch path."
    if classification == "SOURCE_COVERAGE_GAP":
        return "Repair per-symbol source coverage; do not treat this as an EV-only wait."
    if classification == "WAIT_FOR_MARKET_EV":
        return "Market fill and ranking are current; wait for positive EV without lowering thresholds."
    if classification == "WAIT_FOR_EXECUTABLE_BOOK":
        return "Positive EV exists, but executable book/risk gates are not paper-ready yet."
    if classification == "PAPER_READY_REVIEW":
        return "Paper-ready rows exist; operator review is required before paper creation."
    if classification == "NO_POSITIVE_EV":
        return "Keep the crypto watch running; no paper trade is expected until raw EV turns positive."
    if classification == "NO_EXECUTABLE_EV":
        return "Wait for spread/liquidity to clear execution gates; do not lower thresholds."
    if classification == "BLOCKED_BY_ACTIVE_WRITER":
        return "Wait for the active database writer to finish before any local-file evidence workflow."
    if classification == "HEALTHY":
        return "Continue paper-only monitoring; inspect paper-ready candidates manually."
    return f"Investigate crypto watch state {runner_state} with bounded read-only diagnostics."


def _phase3an_classification_with_source_quality(
    classification: str,
    *,
    source_quality: dict[str, Any],
) -> str:
    if classification in {
        "BLOCKED_BY_ACTIVE_WRITER",
        "RUNNING_CYCLE_OVERDUE",
        "WATCHER_STALE",
        "RESTART_SAFE",
        "RESTART_NOT_SAFE",
    }:
        return classification
    source_classification = source_quality_classification_for_phase3an(source_quality)
    if source_classification in {
        "API_RATE_LIMIT_PRESSURE",
        "SOURCE_SERIES_EMPTY",
        "SOURCE_COVERAGE_GAP",
        "WAIT_FOR_MARKET_EV",
        "WAIT_FOR_EXECUTABLE_BOOK",
        "PAPER_READY_REVIEW",
    }:
        return source_classification
    return classification


def _restart_plan_steps(doctor: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "step": "confirm_stale_or_overdue",
            "status": "READY" if doctor.get("classification") in {"WATCHER_STALE", "RESTART_SAFE"} else "REVIEW",
            "command": "kalshi-bot phase3an-crypto-watch-doctor --output-dir reports/phase3an",
        },
        {
            "step": "confirm_no_active_writer",
            "status": "REQUIRED",
            "command": "kalshi-bot db-writer-monitor --json",
        },
        {
            "step": "operator_restart",
            "status": "NOT_EXECUTED_DRY_RUN",
            "command": "kalshi-bot scheduler-plan --profile crypto-watch",
        },
    ]


def _normalize_reason(reason: Any) -> str:
    mapped = {
        "NO_FORECAST": "NO_VALID_FORECAST",
        "CONFIDENCE_TOO_LOW": "CONFIDENCE_BELOW_THRESHOLD",
        "EXPIRED_CRYPTO_WINDOW": "SETTLEMENT_CHECK_FAILED",
        "WAITING_FOR_SETTLEMENT": "SETTLEMENT_CHECK_FAILED",
    }.get(str(reason), str(reason))
    return mapped if mapped in PHASE3AN_FUNNEL_REASON_CODES else "UNKNOWN_REQUIRES_INVESTIGATION"


def _normalize_funnel_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    copied["source_reason_code"] = row.get("reason_code")
    copied["reason_code"] = _normalize_reason(row.get("reason_code"))
    return copied


def _normalized_reason_counts(
    source_counts: Any,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {reason: 0 for reason in PHASE3AN_FUNNEL_REASON_CODES}
    if isinstance(source_counts, dict):
        for reason, count in source_counts.items():
            counts[_normalize_reason(reason)] = counts.get(_normalize_reason(reason), 0) + _intish(count)
    for row in rows:
        reason = _normalize_reason(row.get("reason_code"))
        counts[reason] = max(counts.get(reason, 0), 0)
    return counts


def _normalized_stage_counts(
    source_stages: Any,
    reason_counts: dict[str, int],
) -> list[dict[str, Any]]:
    total = sum(reason_counts.values())
    by_stage = {
        str(row.get("stage")): row
        for row in source_stages
        if isinstance(source_stages, list) and isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    for stage in PHASE3AN_FUNNEL_STAGES:
        source = by_stage.get(stage) or by_stage.get(stage.replace("linked_direct", "parsed_or_link_safe"))
        passed = _intish(_dict(source).get("passed")) if source else 0
        failed = max(total - passed, 0) if total else 0
        rows.append({"stage": stage, "passed": passed, "dropped": failed})
    return rows


def _first_hard_blocker(reason_counts: dict[str, int]) -> str:
    for reason in PHASE3AN_FUNNEL_REASON_CODES:
        if _intish(reason_counts.get(reason)) > 0:
            return reason
    return "NONE"


def _best_negative_ev_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    negatives: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = float(row.get("raw_ev") or row.get("estimated_edge") or 0)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            copied = dict(row)
            copied["_sort_ev"] = value
            negatives.append(copied)
    if not negatives:
        return None
    best = max(negatives, key=lambda row: row["_sort_ev"])
    best.pop("_sort_ev", None)
    return best


def _render_phase3an_paper_funnel_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AN Paper Funnel Explain",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- First hard blocker: `{payload['summary']['first_hard_blocker']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Top Reasons", "", "| Reason | Count |", "| --- | ---: |"])
    for reason, count in payload["top_block_reasons"]:
        lines.append(f"| {reason} | {count} |")
    lines.extend(["", "## Next Action", "", str(payload["exact_next_action"]), ""])
    return "\n".join(lines)


def _open_paper_order_count(session: Session) -> int:
    try:
        return int(session.scalar(select(func.count()).select_from(PaperOrder)) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _recent_paper_pnl_count(session: Session, *, hours: int) -> int:
    cutoff = utc_now() - timedelta(hours=hours)
    try:
        return int(
            session.scalar(
                select(func.count()).select_from(PaperPnl).where(PaperPnl.calculated_at >= cutoff)
            )
            or 0
        )
    except Exception:  # noqa: BLE001
        return 0


def _adapter_count(rows: Any, adapter_key: str) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(1 for row in rows if isinstance(row, dict) and row.get("source_adapter_key") == adapter_key)


def _missing_source_fields_count(rows: Any) -> int:
    if not isinstance(rows, list):
        return 0
    count = 0
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("missing_fields"), list):
            count += len(row["missing_fields"])
    return count


def _review_gated_rows(rows: Any) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and not bool(row.get("review_approved"))
    )


def _safe_rows(rows: Any, key: str) -> int:
    if not isinstance(rows, list):
        return 0
    return sum(1 for row in rows if isinstance(row, dict) and bool(row.get(key)))


def _phase3an_group_source_review_status(
    *,
    template_csv: Path,
    sources_dir: Path,
    writer: Any,
) -> dict[str, Any]:
    output_path = sources_dir / "phase3bb_r2_group_source_review.csv"
    if not template_csv.exists():
        return {
            "status": "HELPER_AVAILABLE_TEMPLATE_MISSING",
            "helper_missing": False,
            "input_path": str(template_csv),
            "output_path": str(output_path),
            "row_count": 0,
            "group_count": 0,
        }
    try:
        artifacts = writer(input_path=template_csv, output_path=output_path)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "GROUP_REVIEW_FAILED_CLOSED",
            "helper_missing": False,
            "error": str(exc),
            "input_path": str(template_csv),
            "output_path": str(output_path),
            "row_count": 0,
            "group_count": 0,
        }
    return {
        "status": "GROUP_REVIEW_WRITTEN",
        "helper_missing": False,
        "input_path": str(template_csv),
        "output_path": str(output_path),
        "row_count": artifacts.row_count,
        "group_count": artifacts.group_count,
    }


def _phase3an_general_source_gate_summary(
    *,
    usda: dict[str, Any],
    source_evidence_ready: int,
    activation: dict[str, Any],
    flightaware_gate: dict[str, Any],
    flightaware_date_stable: dict[str, Any],
) -> dict[str, Any]:
    activation_summary = _dict(activation.get("summary"))
    r4_summary = _dict(flightaware_gate.get("summary"))
    r5_summary = _dict(flightaware_date_stable.get("summary"))
    raw_decisions = activation.get("source_activation_decisions")
    decisions = (
        [row for row in raw_decisions if isinstance(row, dict)]
        if isinstance(raw_decisions, list)
        else []
    )
    usda_decision = _phase3an_source_decision(decisions, "USDA")
    cushman_decision = _phase3an_source_decision(decisions, "Cushman")
    flightaware_decision = _phase3an_source_decision(decisions, "FlightAware")

    usda_rows = _phase3an_decision_rows(usda_decision)
    cushman_rows = _phase3an_decision_rows(cushman_decision)
    flightaware_rows = (
        _intish(r5_summary.get("affected_rows"))
        or _intish(r4_summary.get("affected_rows"))
        or _phase3an_decision_rows(flightaware_decision)
    )
    flightaware_date_stable_rows = _intish(
        r5_summary.get("accepted_date_stable_evidence_rows")
    )
    flightaware_review_rows = (
        flightaware_rows
        if (
            flightaware_rows
            and _phase3an_bool(
                r5_summary.get("source_value_available_for_review")
                if r5_summary
                else r4_summary.get("source_value_available_for_review")
            )
            and flightaware_date_stable_rows == 0
        )
        else _phase3an_review_rows(flightaware_decision)
    )
    wrong_date_rows = (
        usda_rows
        if _phase3an_source_has_blocker(
            usda_decision,
            "SOURCE_DATE_MISMATCH_BLOCKER",
        )
        else 0
    )
    proprietary_rows = (
        cushman_rows
        if _phase3an_source_has_blocker(
            cushman_decision,
            "PROPRIETARY_REVIEW_REQUIRED",
        )
        else 0
    )
    link_safe_rows = _intish(activation_summary.get("link_safe_rows"))
    forecast_safe_rows = _intish(activation_summary.get("forecast_safe_rows"))
    activation_candidate_rows = _intish(activation_summary.get("activation_candidate_rows"))
    total_rows = sum(
        count
        for count in (usda_rows, cushman_rows, flightaware_rows)
        if count > 0
    )
    blocked_rows = max(0, total_rows - flightaware_review_rows - activation_candidate_rows)
    r5_complete = bool(flightaware_date_stable)
    r4_complete = bool(flightaware_gate)
    r3_complete = bool(activation)
    first_hard_blocker = (
        str(r5_summary.get("first_hard_blocker") or "")
        if r5_complete and flightaware_date_stable_rows == 0
        else str(activation_summary.get("first_hard_blocker") or "")
    )
    if not first_hard_blocker:
        first_hard_blocker = (
            "NO_LINK_OR_FORECAST_SAFE_SOURCE_ROWS"
            if total_rows or source_evidence_ready
            else "SOURCE_EVIDENCE_NOT_READY"
        )
    activation_complete = bool(
        r3_complete
        and r4_complete
        and r5_complete
        and link_safe_rows == 0
        and forecast_safe_rows == 0
        and (flightaware_review_rows or blocked_rows or total_rows)
    )
    source_evidence_status = (
        "SOURCE_EVIDENCE_ACTIVATION_READY"
        if link_safe_rows and forecast_safe_rows
        else "SOURCE_EVIDENCE_CLASSIFIED_GATED"
        if activation_complete
        else "SOURCE_EVIDENCE_READY"
        if source_evidence_ready
        else "SOURCE_EVIDENCE_BLOCKED"
    )
    sources = {
        "USDA": _phase3an_source_row(
            source_name="USDA",
            fallback_status=_usda_status_from_report(usda),
            fallback_blocker=str(
                usda.get("current_blocker")
                or "USDA_SOURCE_EVIDENCE_UNRESOLVED"
            ),
            fallback_next_action=str(
                usda.get("next_action")
                or "Review exact USDA evidence before source promotion."
            ),
            decision=usda_decision,
            affected_rows=usda_rows,
            official_free=True,
            date_stable=False,
            date_stable_status=(
                "WRONG_DATE_OR_VALUE_UNAVAILABLE" if wrong_date_rows else "UNPROVEN"
            ),
            review_gated_rows=0,
            blocked_rows=usda_rows,
            status_override=str(usda_decision.get("first_blocker") or "") or None,
        ),
        "Cushman": _phase3an_source_row(
            source_name="Cushman",
            fallback_status="CUSHMAN_VALUES_UNAVAILABLE",
            fallback_blocker=(
                "Cushman values are unavailable and proprietary/licensing review is incomplete."
            ),
            fallback_next_action=(
                "Confirm permissible Cushman source access and reviewed redacted reporting rules."
            ),
            decision=cushman_decision,
            affected_rows=cushman_rows,
            official_free=False,
            date_stable=False,
            date_stable_status="PROPRIETARY_OR_UNAVAILABLE",
            review_gated_rows=0,
            blocked_rows=cushman_rows,
            status_override=str(cushman_decision.get("first_blocker") or "") or None,
        ),
        "FlightAware": _phase3an_source_row(
            source_name="FlightAware",
            fallback_status="FLIGHTAWARE_READY_FOR_REVIEW",
            fallback_blocker=(
                "Entity, airport/route/time-window, freshness, no-leakage, and review "
                "approval tests have not passed."
            ),
            fallback_next_action=(
                "Run report-only FlightAware ambiguity, freshness, and no-leakage review."
            ),
            decision=flightaware_decision,
            affected_rows=flightaware_rows,
            official_free=True,
            date_stable=flightaware_date_stable_rows > 0,
            date_stable_status=str(
                r5_summary.get("date_stable_evidence_status")
                or r4_summary.get("date_stable_evidence_available")
                or "UNPROVEN"
            ),
            review_gated_rows=flightaware_review_rows,
            blocked_rows=0 if flightaware_review_rows else flightaware_rows,
            status_override=(
                str(r5_summary.get("first_hard_blocker") or "")
                if r5_complete and flightaware_date_stable_rows == 0
                else None
            ),
            blocker_override=(
                str(r5_summary.get("next_action") or "") if r5_complete else None
            ),
        ),
    }
    return {
        "source_evidence_status": source_evidence_status,
        "activation_readiness": (
            "READY" if link_safe_rows and forecast_safe_rows else "NOT_READY"
        ),
        "first_hard_blocker": first_hard_blocker,
        "official_free_source_rows": usda_rows + flightaware_rows,
        "date_stable_rows": flightaware_date_stable_rows,
        "date_stable_missing_rows": (
            flightaware_rows
            if r5_complete and not flightaware_date_stable_rows
            else 0
        ),
        "review_gated_rows": flightaware_review_rows,
        "blocked_rows": blocked_rows,
        "proprietary_blocked_rows": proprietary_rows,
        "wrong_date_rows": wrong_date_rows,
        "source_date_mismatch_blockers": bool(
            activation_summary.get("source_date_mismatch_blockers") or wrong_date_rows
        ),
        "proprietary_review_blockers": bool(
            activation_summary.get("proprietary_review_blockers") or proprietary_rows
        ),
        "review_required_blockers": bool(
            activation_summary.get("review_required_blockers") or flightaware_review_rows
        ),
        "link_safe_rows": link_safe_rows,
        "forecast_safe_rows": forecast_safe_rows,
        "activation_candidate_rows": activation_candidate_rows,
        "phase3ax_r5_source_activation_complete": activation_complete,
        "source_activation_decisions": list(sources.values()),
        "sources": sources,
        "next_action": _phase3an_general_source_next_action(
            first_hard_blocker=first_hard_blocker,
            activation_complete=activation_complete,
            link_safe_rows=link_safe_rows,
            forecast_safe_rows=forecast_safe_rows,
        ),
    }


def _phase3an_source_decision(
    decisions: list[dict[str, Any]],
    source_name: str,
) -> dict[str, Any]:
    for decision in decisions:
        if str(decision.get("source_name") or "").lower() == source_name.lower():
            return decision
    return {}


def _phase3an_decision_rows(decision: dict[str, Any]) -> int:
    return _intish(decision.get("affected_rows"))


def _phase3an_review_rows(decision: dict[str, Any]) -> int:
    if not decision:
        return 0
    if _phase3an_source_has_blocker(decision, "READY_FOR_REVIEW_NOT_LINK_SAFE"):
        return _phase3an_decision_rows(decision)
    if _phase3an_bool(decision.get("source_value_available_for_review")):
        return _phase3an_decision_rows(decision)
    return 0


def _phase3an_source_has_blocker(decision: dict[str, Any], blocker: str) -> bool:
    blockers = decision.get("blocker_codes")
    return isinstance(blockers, list) and blocker in blockers


def _phase3an_source_row(
    *,
    source_name: str,
    fallback_status: str,
    fallback_blocker: str,
    fallback_next_action: str,
    decision: dict[str, Any],
    affected_rows: int,
    official_free: bool,
    date_stable: bool,
    date_stable_status: str,
    review_gated_rows: int,
    blocked_rows: int,
    status_override: str | None = None,
    blocker_override: str | None = None,
) -> dict[str, Any]:
    link_safe_rows = _intish(decision.get("link_safe_rows"))
    forecast_safe_rows = _intish(decision.get("forecast_safe_rows"))
    first_blocker = str(decision.get("first_blocker") or decision.get("block_reason") or "")
    return {
        "status": status_override or str(decision.get("activation_status") or fallback_status),
        "source_name": source_name,
        "official_or_free_source": official_free,
        "date_stable": date_stable,
        "date_stable_status": date_stable_status,
        "affected_rows": affected_rows,
        "evidence_ready_rows": _intish(decision.get("evidence_ready_rows")),
        "link_safe": link_safe_rows > 0,
        "forecast_safe": forecast_safe_rows > 0,
        "link_safe_rows": link_safe_rows,
        "forecast_safe_rows": forecast_safe_rows,
        "review_gated_rows": review_gated_rows,
        "blocked_rows": blocked_rows,
        "blocker": blocker_override or first_blocker or fallback_blocker,
        "blocker_codes": (
            decision.get("blocker_codes")
            if isinstance(decision.get("blocker_codes"), list)
            else []
        ),
        "next_action": str(decision.get("next_action") or fallback_next_action),
        "paper_trade_writes": False,
        "live_or_demo_execution": False,
    }


def _phase3an_general_source_next_action(
    *,
    first_hard_blocker: str,
    activation_complete: bool,
    link_safe_rows: int,
    forecast_safe_rows: int,
) -> str:
    if link_safe_rows and forecast_safe_rows:
        return "Run a report-only reviewed source promotion dry run before any source writes."
    if activation_complete:
        return (
            "General source evidence is classified but not link/forecast safe; keep it "
            "diagnostic-only and move to sports provenance repair."
        )
    if first_hard_blocker == "OFFICIAL_FLIGHTAWARE_HISTORICAL_AGGREGATE_UNAVAILABLE":
        return "External FlightAware historical aggregate access is required before promotion."
    if first_hard_blocker == "SOURCE_DATE_MISMATCH_BLOCKER":
        return "Resolve exact USDA July 3 evidence before any commodity link or forecast work."
    return "Review exact source evidence rows; no links or forecasts are safe by default."


def _phase3an_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _phase3an_source_next_action(general_sources: dict[str, Any]) -> str:
    sources = _dict(general_sources.get("sources"))
    usda = _dict(sources.get("USDA"))
    cushman = _dict(sources.get("Cushman"))
    flightaware = _dict(sources.get("FlightAware"))
    if usda.get("status") in {"USDA_DATE_MISMATCH", "USDA_EXACT_REPORT_NOT_FOUND"}:
        return "Resolve exact USDA July 3 evidence before any commodity link or forecast work."
    if cushman.get("status") == "CUSHMAN_VALUES_UNAVAILABLE":
        return "Resolve Cushman source values and proprietary review before promotion."
    if flightaware.get("status") == "FLIGHTAWARE_READY_FOR_REVIEW":
        return "Run FlightAware mapping, freshness, no-leakage, and review tests before promotion."
    return "Review exact source evidence rows; no links or forecasts are safe by default."


def _records_from_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else payload
    return [row for row in records if isinstance(row, dict)] if isinstance(records, list) else []


def _general_market_tickers(session: Session, *, terms: tuple[str, ...]) -> list[str]:
    try:
        rows = session.scalars(select(Market).limit(500)).all()
    except Exception:  # noqa: BLE001
        return []
    result: list[str] = []
    for market in rows:
        text = " ".join(
            str(part or "").lower()
            for part in (market.ticker, market.title, market.subtitle, market.rules_primary)
        )
        if any(term in text for term in terms):
            result.append(market.ticker)
    return sorted(result)[:50]


def _usda_status_from_report(report: dict[str, Any]) -> str:
    blocker = str(report.get("current_blocker") or "")
    if blocker == "USDA_VALUES_AVAILABLE_FOR_REVIEW":
        return "SOURCE_EVIDENCE_READY"
    if blocker in {"USDA_DATE_MISMATCH", "USDA_EXACT_REPORT_NOT_FOUND", "USDA_VALUES_UNAVAILABLE"}:
        return blocker
    return "SOURCE_EVIDENCE_BLOCKED"


def _sports_reason_codes(summary: dict[str, Any], existing: Any) -> list[str]:
    codes: set[str] = set()
    if isinstance(existing, list):
        codes.update(str(code) for code in existing)
    elif isinstance(existing, dict):
        codes.update(str(code) for code, count in existing.items() if _intish(count))
    if _intish(summary.get("unresolved_round_placeholders")) or _intish(summary.get("placeholder_rows")):
        codes.add("ROUND_PLACEHOLDER")
    if not bool(summary.get("schedule_evidence_available")):
        codes.add("SCHEDULE_NOT_AVAILABLE")
        codes.add("WAITING_FOR_SOURCE_SCHEDULE")
    if not bool(summary.get("roster_team_evidence_available")):
        codes.add("ROSTER_NOT_AVAILABLE")
    if _intish(summary.get("partial_provenance_sports_markets")):
        codes.add("PARTIAL_PROVENANCE_ONLY")
    if _intish(summary.get("safe_repair_rows")) == 0:
        codes.add("NO_SAFE_REPAIR_ROW")
    if not codes:
        codes.add("NEEDS_HUMAN_REVIEW")
    allowed = {
        "ROUND_PLACEHOLDER",
        "TEAM_UNKNOWN",
        "SCHEDULE_NOT_AVAILABLE",
        "ROSTER_NOT_AVAILABLE",
        "AMBIGUOUS_MATCH",
        "PARTIAL_PROVENANCE_ONLY",
        "NO_SAFE_REPAIR_ROW",
        "WAITING_FOR_SOURCE_SCHEDULE",
        "NEEDS_HUMAN_REVIEW",
    }
    return sorted(code for code in codes if code in allowed)


def _domain_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("readiness_status") or "WAITING_FOR_COMPATIBLE_MARKETS")


def _assess_economic_news_parser_backfill_row(
    session: Session,
    domain: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    from kalshi_predictor.market_legs import parse_market_legs

    ticker = str(row.get("ticker") or "")
    market = session.get(Market, ticker) if ticker else None
    if market is None:
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="MARKET_ROW_NOT_FOUND",
        )

    existing_legs = list(
        session.scalars(
            select(MarketLeg)
            .where(MarketLeg.ticker == ticker)
            .order_by(MarketLeg.leg_index, MarketLeg.id)
        )
    )
    parsed_legs = parse_market_legs(market)
    matching_legs = [leg for leg in parsed_legs if leg.category == domain]
    best_leg = _best_parser_candidate(matching_legs)
    base = {
        "existing_market_leg_count": len(existing_legs),
        "existing_market_leg_indices": [leg.leg_index for leg in existing_legs],
        "parsed_leg_count": len(parsed_legs),
        "parser_candidate_count": len(matching_legs),
        "parser_candidates": [_parser_leg_summary(leg) for leg in matching_legs[:3]],
        "candidate_parser_leg": _parser_leg_summary(best_leg) if best_leg else None,
    }
    if existing_legs:
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="EXISTING_LEG_RECLASSIFICATION_REQUIRES_REVIEW",
            parser_reason_codes=["EXISTING_MARKET_LEG_PRESENT"],
            **base,
        )
    if best_leg is None:
        categories = sorted({leg.category for leg in parsed_legs})
        reason = (
            "PARSER_CATEGORY_MISMATCH"
            if categories
            else "NO_DETERMINISTIC_ECONOMIC_NEWS_LEG_PARSER"
        )
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason=reason,
            parser_reason_codes=[f"PARSER_CATEGORIES:{','.join(categories) or 'none'}"],
            **base,
        )
    if domain == "news":
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="NEWS_PARSER_BACKFILL_REQUIRES_NEWS_ITEM_REVIEW",
            parser_reason_codes=["NEWS_LINK_REQUIRES_EXACT_NEWS_ITEM_REVIEW"],
            **base,
        )

    expected_events = _economic_expected_parser_events(row.get("link_reference"))
    parsed_event = _normalize_economic_parser_event(best_leg.entity_name)
    if not expected_events:
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="ECONOMIC_LINK_EVENT_UNKNOWN",
            parser_reason_codes=[f"PARSER_EVENT:{parsed_event or 'unknown'}"],
            **base,
        )
    if parsed_event not in expected_events:
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="LINK_PARSER_EVENT_MISMATCH",
            parser_reason_codes=[
                f"LINK_EXPECTED:{','.join(sorted(expected_events))}",
                f"PARSER_EVENT:{parsed_event or 'unknown'}",
            ],
            **base,
        )
    if _floatish(best_leg.confidence) < 0.75:
        return _parser_backfill_assessment(
            safe=False,
            unsafe_reason="PARSER_CONFIDENCE_TOO_LOW",
            parser_reason_codes=[f"PARSER_CONFIDENCE:{best_leg.confidence}"],
            **base,
        )
    return _parser_backfill_assessment(
        safe=True,
        unsafe_reason=None,
        parser_reason_codes=[
            "EXACT_LINK_EVENT_MATCHED_DETERMINISTIC_PARSER",
            f"PARSER_EVENT:{parsed_event}",
        ],
        **base,
    )


def _parser_backfill_assessment(
    *,
    safe: bool,
    unsafe_reason: str | None,
    parser_reason_codes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "safe_to_backfill_parser_leg": safe,
        "unsafe_reason": unsafe_reason,
        "parser_reason_codes": parser_reason_codes or [],
        "backfill_policy": (
            "DRY_RUN_ONLY_OPERATOR_APPROVAL_REQUIRED"
            if safe
            else "REPORT_ONLY_REQUIRES_DETERMINISTIC_PARSER_OR_OPERATOR_REVIEW"
        ),
        **extra,
    }


def _best_parser_candidate(candidates: list[Any]) -> Any | None:
    if not candidates:
        return None
    return max(candidates, key=lambda leg: _floatish(getattr(leg, "confidence", None)))


def _parser_leg_summary(leg: Any | None) -> dict[str, Any] | None:
    if leg is None:
        return None
    return {
        "leg_index": leg.leg_index,
        "side": leg.side,
        "category": leg.category,
        "market_type": leg.market_type,
        "entity_name": leg.entity_name,
        "operator": leg.operator,
        "threshold_value": leg.threshold_value,
        "unit": leg.unit,
        "confidence": leg.confidence,
        "raw_text": leg.raw_text,
        "reason": leg.reason,
    }


def _economic_expected_parser_events(link_reference: Any) -> set[str]:
    if not isinstance(link_reference, dict):
        return set()
    raw_event = str(link_reference.get("event_key") or "").strip().lower()
    normalized = raw_event.replace("-", "_").replace(" ", "_")
    if not normalized:
        return set()
    if any(token in normalized for token in ("cpi", "inflation")):
        return {"cpi"}
    if any(token in normalized for token in ("fed", "fomc", "interest", "rate")):
        return {"fomc"}
    if any(token in normalized for token in ("jobs", "payroll", "unemployment", "labor")):
        return {"jobs"}
    if "gdp" in normalized:
        return {"gdp"}
    return set()


def _normalize_economic_parser_event(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"cpi", "fomc", "jobs", "gdp"}:
        return normalized
    if normalized in {"inflation", "core_cpi", "headline_cpi"}:
        return "cpi"
    if normalized in {"fed", "federal_reserve", "interest_rate", "rates"}:
        return "fomc"
    if normalized in {"unemployment", "payrolls", "jobs_report"}:
        return "jobs"
    return None


def _floatish(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parser_backfill_reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        reason = (
            "SAFE_TO_BACKFILL_DRY_RUN"
            if row.get("safe_to_backfill_parser_leg") is True
            else str(row.get("unsafe_reason") or "UNKNOWN_UNSAFE_REASON")
        )
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _parser_backfill_first_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_CURRENT_EXACT_LINKS_WITHOUT_PARSED_LEG"
    counts = _parser_backfill_reason_counts(rows)
    for reason in (
        "LINK_PARSER_EVENT_MISMATCH",
        "EXISTING_LEG_RECLASSIFICATION_REQUIRES_REVIEW",
        "NEWS_PARSER_BACKFILL_REQUIRES_NEWS_ITEM_REVIEW",
        "ECONOMIC_LINK_EVENT_UNKNOWN",
        "PARSER_CATEGORY_MISMATCH",
        "PARSER_CONFIDENCE_TOO_LOW",
        "NO_DETERMINISTIC_ECONOMIC_NEWS_LEG_PARSER",
        "MARKET_ROW_NOT_FOUND",
    ):
        if counts.get(reason):
            return reason
    if counts.get("SAFE_TO_BACKFILL_DRY_RUN"):
        return "PARSER_BACKFILL_READY_DRY_RUN_ONLY"
    return "UNKNOWN_PARSER_BACKFILL_BLOCKER"


def _parser_backfill_next_action(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No current exact-linked economic/news markets need parser backfill."
    counts = _parser_backfill_reason_counts(rows)
    if counts.get("LINK_PARSER_EVENT_MISMATCH"):
        return (
            "Review exact economic links whose parser event disagrees with the link "
            "event; do not write parser legs for mismatched rows."
        )
    if counts.get("SAFE_TO_BACKFILL_DRY_RUN"):
        return (
            "Parser-safe rows exist in dry-run; run db-writer-monitor before any "
            "future operator-approved exact parser backfill."
        )
    return (
        "Resolve parser/link review blockers in economic_news_parser_backfill_plan.json; "
        "keep forecasts and paper trades blocked."
    )


def _economic_link_event_repair_row(row: dict[str, Any]) -> dict[str, Any]:
    current_event_key = _current_link_event_key(row)
    normalized_link_events = _economic_expected_parser_events(row.get("link_reference"))
    normalized_link_event = ",".join(sorted(normalized_link_events)) or None
    candidate = _dict(row.get("candidate_parser_leg"))
    parser_event = _normalize_economic_parser_event(candidate.get("entity_name"))
    suggested_event_key = parser_event if parser_event in {"cpi", "fomc", "jobs", "gdp"} else None
    confidence = _floatish(candidate.get("confidence"))
    is_mismatch = row.get("unsafe_reason") == "LINK_PARSER_EVENT_MISMATCH"
    deterministic_parser = parser_event is not None and confidence >= 0.75
    safe_to_repair = bool(is_mismatch and deterministic_parser and suggested_event_key)
    safe_after_repair = bool(
        safe_to_repair
        and row.get("existing_market_leg_count") == 0
        and row.get("parser_candidate_count")
    )
    reason_codes = list(row.get("parser_reason_codes") or [])
    if safe_to_repair:
        reason_codes.append("EXACT_LINK_EVENT_REPAIR_CANDIDATE")
    elif row.get("safe_to_backfill_parser_leg") is True:
        reason_codes.append("EXACT_LINK_EVENT_ALREADY_MATCHES_PARSER")
    else:
        reason_codes.append(str(row.get("unsafe_reason") or "REVIEW_REQUIRED"))
    return {
        "ticker": row.get("ticker"),
        "title": row.get("title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "close_time": row.get("close_time"),
        "current_event_key": current_event_key,
        "normalized_link_event": normalized_link_event,
        "parser_event": parser_event,
        "suggested_event_key": suggested_event_key,
        "safe_to_repair_link_event": safe_to_repair,
        "safe_to_backfill_parser_leg": row.get("safe_to_backfill_parser_leg") is True,
        "safe_to_backfill_parser_leg_after_link_repair": safe_after_repair,
        "unsafe_reason": row.get("unsafe_reason"),
        "link_event_repair_policy": (
            "REPORT_ONLY_OPERATOR_APPROVAL_REQUIRED"
            if safe_to_repair
            else "NO_LINK_EVENT_WRITE"
        ),
        "parser_backfill_policy": (
            "OPERATOR_GATED_AFTER_DB_WRITER_CLEAR_AND_BACKUP"
            if row.get("safe_to_backfill_parser_leg") is True or safe_after_repair
            else "BLOCKED_UNTIL_LINK_EVENT_REVIEW"
        ),
        "parser_reason_codes": reason_codes,
        "candidate_parser_leg": row.get("candidate_parser_leg"),
        "link_reference": row.get("link_reference"),
    }


def _current_link_event_key(row: dict[str, Any]) -> str | None:
    link_reference = row.get("link_reference")
    if not isinstance(link_reference, dict):
        return None
    value = link_reference.get("event_key")
    return str(value) if value not in (None, "") else None


def _count_rows(rows: list[dict[str, Any]], key: str, value: Any) -> int:
    return sum(1 for row in rows if row.get(key) == value)


def _count_values_simple(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "NONE")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _economic_link_event_first_blocker(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_ECONOMIC_LINK_EVENT_ROWS_TO_REVIEW"
    if any(row["safe_to_repair_link_event"] is True for row in rows):
        return "LINK_EVENT_REPAIR_REQUIRES_OPERATOR_APPROVAL"
    if any(row["safe_to_backfill_parser_leg"] is True for row in rows):
        return "PARSER_BACKFILL_READY_DRY_RUN_ONLY"
    return "NO_SAFE_LINK_EVENT_REPAIR_ROWS"


def _economic_link_event_next_action(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No current economic exact-link parser rows need event repair review."
    if any(row["safe_to_repair_link_event"] is True for row in rows):
        return (
            "Review economic_link_event_repair_plan.json, approve exact link-event "
            "repairs only for deterministic parser matches, then rerun parser backfill "
            "after db-writer-monitor is clear."
        )
    if any(row["safe_to_backfill_parser_leg"] is True for row in rows):
        return (
            "Exact link events already match parser output for safe rows; keep parser "
            "backfill operator-gated and run db-writer-monitor before any future apply."
        )
    return "Keep economic/news forecasts blocked until exact link/parser events agree."


def _latest_economic_link(session: Session, ticker: str) -> EconomicMarketLink | None:
    return session.scalars(
        select(EconomicMarketLink)
        .where(EconomicMarketLink.ticker == ticker)
        .order_by(desc(EconomicMarketLink.detected_at), desc(EconomicMarketLink.id))
        .limit(1)
    ).first()


def _economic_link_event_apply_first_blocker(
    *,
    apply: bool,
    status: str,
    candidates: list[dict[str, Any]],
) -> str:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "BLOCKED_BY_ACTIVE_WRITER"
    if not candidates:
        return "NO_SAFE_LINK_EVENT_REPAIR_ROWS"
    if not apply:
        return "DRY_RUN_OPERATOR_APPROVAL_REQUIRED"
    if status == "APPLIED":
        return "LINK_EVENT_REPAIR_APPLIED"
    return status


def _economic_link_event_apply_next_action(
    *,
    apply: bool,
    status: str,
    candidates: list[dict[str, Any]],
) -> str:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "Wait for db-writer-monitor to clear, then rerun the dry-run."
    if not candidates:
        return "No exact economic link-event repair candidates are currently safe."
    if not apply:
        return (
            "Review dry-run rows; apply later only with --apply --backup-first "
            "after db-writer-monitor is clear."
        )
    if status == "APPLIED":
        return (
            "Rerun phase3an-economic-news-parser-backfill-plan and keep parser "
            "backfill operator-gated."
        )
    return "Review economic_link_event_repair_apply.json before any downstream parser work."


def _economic_parser_leg_backfill_row(row: dict[str, Any]) -> dict[str, Any]:
    candidate = _dict(row.get("candidate_parser_leg"))
    parser_event = _normalize_economic_parser_event(candidate.get("entity_name"))
    current_event_key = _current_link_event_key(row)
    required_missing = _parser_leg_candidate_missing_fields(candidate)
    already_safe = row.get("safe_to_backfill_parser_leg") is True
    safe_to_write = bool(already_safe and not required_missing)
    blocked_reason = None
    if not safe_to_write:
        blocked_reason = (
            "PARSER_CANDIDATE_INCOMPLETE"
            if already_safe and required_missing
            else str(row.get("unsafe_reason") or "NOT_SAFE_TO_BACKFILL_PARSER_LEG")
        )
    return {
        "ticker": row.get("ticker"),
        "title": row.get("title"),
        "event_ticker": row.get("event_ticker"),
        "series_ticker": row.get("series_ticker"),
        "close_time": row.get("close_time"),
        "current_event_key": current_event_key,
        "parser_event": parser_event,
        "safe_to_write_parser_leg": safe_to_write,
        "blocked_reason": blocked_reason,
        "existing_market_leg_count": row.get("existing_market_leg_count"),
        "parser_candidate_count": row.get("parser_candidate_count"),
        "candidate_missing_fields": required_missing,
        "link_parser_events_agree": already_safe,
        "parser_reason_codes": row.get("parser_reason_codes") or [],
        "candidate_parser_leg": row.get("candidate_parser_leg"),
        "link_reference": row.get("link_reference"),
        "backfill_policy": (
            "DRY_RUN_ONLY_OPERATOR_APPROVAL_REQUIRED"
            if safe_to_write
            else "BLOCKED_UNTIL_EXACT_LINK_AND_PARSER_AGREE"
        ),
    }


def _parser_leg_candidate_missing_fields(candidate: dict[str, Any]) -> list[str]:
    required = (
        "leg_index",
        "side",
        "category",
        "market_type",
        "operator",
        "confidence",
        "raw_text",
        "reason",
    )
    return [field for field in required if candidate.get(field) in (None, "")]


def _parser_leg_backfill_blocked_reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.get("safe_to_write_parser_leg") is True:
            continue
        reason = str(row.get("blocked_reason") or "UNKNOWN_PARSER_BACKFILL_BLOCKER")
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _economic_parser_leg_backfill_first_blocker(
    *,
    apply: bool,
    status: str,
    candidates: list[dict[str, Any]],
    blocked_reason_counts: dict[str, int],
) -> str:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "BLOCKED_BY_ACTIVE_WRITER"
    if candidates and not apply:
        return "DRY_RUN_OPERATOR_APPROVAL_REQUIRED"
    if status == "APPLIED":
        return "PARSER_LEG_BACKFILL_APPLIED"
    for reason in (
        "LINK_PARSER_EVENT_MISMATCH",
        "PARSER_CANDIDATE_INCOMPLETE",
        "EXISTING_LEG_RECLASSIFICATION_REQUIRES_REVIEW",
        "NEWS_PARSER_BACKFILL_REQUIRES_NEWS_ITEM_REVIEW",
        "ECONOMIC_LINK_EVENT_UNKNOWN",
        "PARSER_CATEGORY_MISMATCH",
        "PARSER_CONFIDENCE_TOO_LOW",
        "NO_DETERMINISTIC_ECONOMIC_NEWS_LEG_PARSER",
        "MARKET_ROW_NOT_FOUND",
    ):
        if blocked_reason_counts.get(reason):
            return reason
    if not candidates:
        return "NO_SAFE_PARSER_BACKFILL_ROWS"
    return status


def _economic_parser_leg_backfill_next_action(
    *,
    apply: bool,
    status: str,
    candidates: list[dict[str, Any]],
    blocked_reason_counts: dict[str, int],
) -> str:
    if status == "BLOCKED_BY_ACTIVE_WRITER":
        return "Wait for db-writer-monitor to clear, then rerun the parser-leg dry-run."
    if candidates and not apply:
        return (
            "Review economic_parser_leg_backfill_dry_run.json; apply later only with "
            "--apply --backup-first after db-writer-monitor is clear."
        )
    if status == "APPLIED":
        return (
            "Rerun phase3an-economic-news-watch and phase3an-gap-fix-report; keep "
            "forecasts, opportunities, and paper trades gated."
        )
    if blocked_reason_counts.get("LINK_PARSER_EVENT_MISMATCH"):
        return (
            "Review economic_link_event_repair_plan.json first; parser legs stay "
            "blocked until exact link and parser events agree."
        )
    return "No safe economic parser-leg backfill rows are available right now."


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _economic_operator_approval_packet_status(
    *,
    link_candidates: list[dict[str, Any]],
    parser_candidates: list[dict[str, Any]],
    writer: dict[str, Any],
) -> str:
    if not link_candidates and not parser_candidates:
        return "NO_OPERATOR_APPROVAL_CANDIDATES"
    if not bool(writer.get("safe_to_start_write", True)):
        return "READY_FOR_OPERATOR_REVIEW_WRITER_BUSY"
    return "READY_FOR_OPERATOR_REVIEW"


def _economic_operator_approval_packet_first_blocker(
    *,
    status: str,
    link_candidates: list[dict[str, Any]],
    parser_candidates: list[dict[str, Any]],
    blocked_reason_counts: dict[str, Any],
) -> str:
    if status == "READY_FOR_OPERATOR_REVIEW_WRITER_BUSY":
        return "OPERATOR_REVIEW_READY_BUT_DB_WRITER_ACTIVE"
    if link_candidates or parser_candidates:
        return "OPERATOR_REVIEW_REQUIRED"
    if blocked_reason_counts.get("LINK_PARSER_EVENT_MISMATCH"):
        return "LINK_PARSER_EVENT_MISMATCH"
    return "NO_OPERATOR_APPROVAL_CANDIDATES"


def _economic_operator_approval_next_action(
    *,
    status: str,
    link_candidates: list[dict[str, Any]],
    parser_candidates: list[dict[str, Any]],
) -> str:
    if not link_candidates and not parser_candidates:
        return "No economic operator approval candidates are available; keep watching."
    if status == "READY_FOR_OPERATOR_REVIEW_WRITER_BUSY":
        return (
            "Review the packet now, but wait for db-writer-monitor to clear before "
            "any later --apply --backup-first command."
        )
    return (
        "Review ECONOMIC_OPERATOR_APPROVAL_PACKET.md; if approved later, run only "
        "the registered command sequence with --backup-first and bounded max records."
    )


def _economic_operator_approval_command_sequence(
    *,
    output_dir: Path,
    limit: int,
    max_records: int,
) -> list[str]:
    out = str(output_dir)
    return [
        "kalshi-bot db-writer-monitor",
        (
            "kalshi-bot phase3an-economic-link-event-repair "
            f"--output-dir {out} --limit {limit} --max-records {max_records}"
        ),
        (
            "AFTER HUMAN APPROVAL ONLY: kalshi-bot phase3an-economic-link-event-repair "
            f"--output-dir {out} --limit {limit} --max-records {max_records} "
            "--apply --backup-first"
        ),
        (
            "kalshi-bot phase3an-economic-news-parser-backfill-plan "
            f"--output-dir {out} --limit {limit}"
        ),
        (
            "kalshi-bot phase3an-economic-parser-leg-backfill "
            f"--output-dir {out} --limit {limit} --max-records {max_records}"
        ),
        (
            "AFTER HUMAN APPROVAL ONLY: kalshi-bot phase3an-economic-parser-leg-backfill "
            f"--output-dir {out} --limit {limit} --max-records {max_records} "
            "--apply --backup-first"
        ),
        "kalshi-bot phase3an-gap-fix-report --output-dir reports/phase3an --reports-dir reports",
    ]


def _candidate_tickers(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("ticker")) for row in rows if row.get("ticker")]


def _compact_link_event_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "candidate_rows": payload.get("candidate_rows"),
        "written_rows": payload.get("written_rows"),
        "exact_next_action": payload.get("exact_next_action"),
    }


def _compact_parser_leg_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "candidate_rows": payload.get("candidate_rows"),
        "blocked_rows": payload.get("blocked_rows"),
        "written_rows": payload.get("written_rows"),
        "exact_next_action": payload.get("exact_next_action"),
    }


def _render_phase3an_economic_operator_approval_packet_markdown(
    payload: dict[str, Any],
) -> str:
    summary = _dict(payload.get("summary"))
    commands = list(payload.get("registered_operator_command_sequence") or [])
    checklist = list(payload.get("review_checklist") or [])
    lines = [
        "# Phase 3AN-R5 Economic Operator Approval Packet",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Status: `{payload.get('status')}`",
        f"- First blocker: `{summary.get('first_blocker')}`",
        f"- Link repair candidates: `{summary.get('link_repair_candidates')}`",
        f"- Parser backfill candidates: `{summary.get('parser_backfill_candidates')}`",
        f"- Parser blocked rows: `{summary.get('parser_blocked_rows')}`",
        f"- Link rows written: `{summary.get('link_rows_written')}`",
        f"- Parser rows written: `{summary.get('parser_rows_written')}`",
        f"- Paper trades created: `{summary.get('paper_trades_created')}`",
        f"- DB writer safe now: `{summary.get('db_writer_safe_to_start')}`",
        "",
        "## Safety",
        "",
        "- Report only.",
        "- No live/demo exchange execution.",
        "- No order submit/cancel/replace.",
        "- No paper trade creation.",
        "- No automatic apply.",
        "",
        "## Review Checklist",
        "",
    ]
    lines.extend(f"- {item}" for item in checklist)
    lines.extend(["", "## Registered Command Sequence", ""])
    lines.extend(f"{index}. `{command}`" for index, command in enumerate(commands, start=1))
    lines.extend(["", f"Next action: {payload.get('exact_next_action')}"])
    return "\n".join(lines) + "\n"


def _audit_economic_operator_commands(commands: list[str]) -> dict[str, Any]:
    allowed = {
        "db-writer-monitor",
        "phase3an-economic-link-event-repair",
        "phase3an-economic-news-parser-backfill-plan",
        "phase3an-economic-parser-leg-backfill",
        "phase3an-gap-fix-report",
    }
    reviewed: list[dict[str, Any]] = []
    unregistered: list[str] = []
    unguarded_apply: list[str] = []
    apply_missing_backup: list[str] = []
    failures: list[str] = []
    for command in commands:
        command_name = _extract_kalshi_bot_command_name(command)
        human_approval_prefix = command.strip().startswith("AFTER HUMAN APPROVAL ONLY:")
        is_apply = " --apply" in f" {command} "
        has_backup = " --backup-first" in f" {command} "
        if command_name not in allowed:
            unregistered.append(command)
        if is_apply and not human_approval_prefix:
            unguarded_apply.append(command)
        if is_apply and not has_backup:
            apply_missing_backup.append(command)
        reviewed.append(
            {
                "command": command,
                "command_name": command_name,
                "registered_known_command": command_name in allowed,
                "apply_command": is_apply,
                "human_approval_prefix": human_approval_prefix,
                "backup_first_present": has_backup,
            }
        )
    if unregistered:
        failures.append("UNREGISTERED_OPERATOR_COMMAND")
    if unguarded_apply:
        failures.append("UNGUARDED_APPLY_COMMAND")
    if apply_missing_backup:
        failures.append("APPLY_COMMAND_MISSING_BACKUP_FIRST")
    return {
        "reviewed_commands": reviewed,
        "allowed_registered_commands": sorted(allowed),
        "unregistered_commands": unregistered,
        "unguarded_apply_commands": unguarded_apply,
        "apply_commands_missing_backup_first": apply_missing_backup,
        "failures": failures,
    }


def _extract_kalshi_bot_command_name(command: str) -> str | None:
    parts = command.split()
    for index, part in enumerate(parts):
        if part == "kalshi-bot" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _economic_packet_source_write_audit(source_dry_runs: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    summaries: dict[str, Any] = {}
    for key in ("link_event_repair", "parser_leg_backfill"):
        source = _dict(source_dry_runs.get(key))
        summary = _dict(source.get("summary"))
        write_counts = {
            "link_rows_written": _intish(summary.get("link_rows_written")),
            "parser_rows_written": _intish(summary.get("parser_rows_written")),
            "paper_trades_created": _intish(summary.get("paper_trades_created")),
        }
        if any(value != 0 for value in write_counts.values()):
            failures.append(f"{key.upper()}_SOURCE_DRY_RUN_WROTE_ROWS")
        summaries[key] = {
            "mode": source.get("mode"),
            "status": source.get("status"),
            "write_counts": write_counts,
        }
    return {"summaries": summaries, "failures": failures}


def _render_phase3an_economic_approval_safety_guard_markdown(
    payload: dict[str, Any],
) -> str:
    summary = _dict(payload.get("summary"))
    failures = list(payload.get("guard_failures") or [])
    lines = [
        "# Phase 3AN-R6 Economic Approval Safety Guard",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Guard status: `{payload.get('guard_status')}`",
        f"- Approval packet status: `{summary.get('approval_packet_status')}`",
        f"- Commands reviewed: `{summary.get('commands_reviewed')}`",
        f"- Unregistered commands: `{len(summary.get('unregistered_commands') or [])}`",
        f"- Unguarded apply commands: `{len(summary.get('unguarded_apply_commands') or [])}`",
        (
            "- Apply commands missing backup-first: "
            f"`{len(summary.get('apply_commands_missing_backup_first') or [])}`"
        ),
        f"- Source write failures: `{len(summary.get('source_write_failures') or [])}`",
        f"- First blocker: `{summary.get('first_blocker')}`",
        "",
        "## Safety",
        "",
        "- Report only.",
        "- No live/demo exchange execution.",
        "- No order submit/cancel/replace.",
        "- No paper trade creation.",
        "- No automatic apply.",
        "",
        "## Failures",
        "",
    ]
    if failures:
        lines.extend(f"- `{failure}`" for failure in failures)
    else:
        lines.append("- None")
    lines.extend(["", f"Next action: {payload.get('exact_next_action')}"])
    return "\n".join(lines) + "\n"


def _morning_handoff_status(
    *,
    packet: dict[str, Any],
    guard: dict[str, Any],
    r5_status: dict[str, Any],
) -> str:
    if not packet:
        return "MISSING_APPROVAL_PACKET"
    if not guard:
        return "MISSING_APPROVAL_SAFETY_GUARD"
    if guard.get("guard_status") != "PASS_REPORT_ONLY":
        return "SAFETY_GUARD_FAILED"
    if packet.get("status") != "READY_FOR_OPERATOR_REVIEW":
        return "APPROVAL_PACKET_NOT_READY"
    r5_guard = _dict(r5_status.get("guard"))
    if r5_status and r5_guard.get("running") is not True:
        return "R5_WATCHER_NOT_RUNNING"
    return "READY_FOR_MORNING_OPERATOR_REVIEW"


def _morning_handoff_hard_stops(
    *,
    packet: dict[str, Any],
    guard: dict[str, Any],
) -> list[str]:
    hard_stops: list[str] = []
    if not packet:
        hard_stops.append("MISSING_APPROVAL_PACKET")
    if not guard:
        hard_stops.append("MISSING_APPROVAL_SAFETY_GUARD")
    if guard and guard.get("guard_status") != "PASS_REPORT_ONLY":
        hard_stops.append("APPROVAL_SAFETY_GUARD_NOT_PASSING")
    if packet and packet.get("auto_apply_supported") is not False:
        hard_stops.append("AUTO_APPLY_NOT_ALLOWED")
    if packet and packet.get("live_or_demo_execution") is not False:
        hard_stops.append("LIVE_OR_DEMO_EXECUTION_NOT_ALLOWED")
    if packet and packet.get("paper_trade_creation") is not False:
        hard_stops.append("PAPER_TRADE_CREATION_NOT_ALLOWED")
    return hard_stops


def _morning_handoff_next_action(*, status: str, hard_stop_reasons: list[str]) -> str:
    if hard_stop_reasons:
        return "Resolve the hard-stop reasons before reviewing any apply command."
    if status == "READY_FOR_MORNING_OPERATOR_REVIEW":
        return (
            "Review the morning handoff, approval packet, and safety guard; do not run "
            "any --apply --backup-first command without explicit human approval."
        )
    return "Refresh the missing or stale source artifacts, then regenerate the morning handoff."


def _render_phase3an_economic_morning_operator_handoff_markdown(
    payload: dict[str, Any],
) -> str:
    summary = _dict(payload.get("summary"))
    hard_stops = list(payload.get("hard_stop_reasons") or [])
    review_order = list(payload.get("review_order") or [])
    commands = list(payload.get("registered_next_commands") or [])
    lines = [
        "# Phase 3AN-R7 Morning Operator Handoff",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Status: `{payload.get('status')}`",
        f"- First blocker: `{summary.get('first_blocker')}`",
        f"- Approval packet: `{summary.get('approval_packet_status')}`",
        f"- Approval safety guard: `{summary.get('approval_guard_status')}`",
        f"- Link repair candidates: `{summary.get('link_repair_candidates')}`",
        f"- Parser backfill candidates: `{summary.get('parser_backfill_candidates')}`",
        f"- Parser blocked rows: `{summary.get('parser_blocked_rows')}`",
        f"- R5: `{summary.get('r5_status')}` cycle `{summary.get('r5_cycle_number')}/{summary.get('r5_total_cycles')}`",
        f"- Positive EV rows: `{summary.get('positive_ev_rows')}`",
        f"- Paper-ready candidates: `{summary.get('paper_ready_candidates')}`",
        f"- Market refresh status: `{summary.get('market_refresh_status')}`",
        f"- Markets/snapshots/forecasts: `{summary.get('markets_seen')}/{summary.get('snapshots_inserted')}/{summary.get('forecasts_inserted')}`",
        f"- Paper health: `{summary.get('paper_health_status')}`",
        f"- Eligible exact settlements: `{summary.get('eligible_exact_settlements')}`",
        "",
        "## Safety",
        "",
        "- Report only.",
        "- No live/demo exchange execution.",
        "- No order submit/cancel/replace.",
        "- No paper trade creation.",
        "- No automatic apply.",
        "",
        "## Hard Stops",
        "",
    ]
    lines.extend(f"- `{reason}`" for reason in hard_stops) if hard_stops else lines.append("- None")
    lines.extend(["", "## Review Order", ""])
    lines.extend(f"{index}. {item}" for index, item in enumerate(review_order, start=1))
    lines.extend(["", "## Registered Next Commands", ""])
    lines.extend(f"{index}. `{command}`" for index, command in enumerate(commands, start=1))
    lines.extend(["", f"Next action: {payload.get('exact_next_action')}"])
    return "\n".join(lines) + "\n"


def _overnight_continuity_flags(
    *,
    r5_status: dict[str, Any],
    health: dict[str, Any],
    handoff: dict[str, Any],
    guard: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    r5_guard = _dict(r5_status.get("guard"))
    r5_latest = _dict(r5_status.get("latest_summary"))
    paper_health = _dict(health.get("paper_health"))
    if not r5_status:
        flags.append("MISSING_R5_STATUS")
    elif r5_guard.get("running") is not True:
        flags.append("R5_NOT_RUNNING")
    if _intish(r5_latest.get("paper_ready_candidates")) > 0:
        flags.append("PAPER_READY_CANDIDATES_REQUIRE_OPERATOR_REVIEW")
    if _intish(r5_latest.get("positive_ev_rows")) > 0:
        flags.append("POSITIVE_EV_ROWS_REQUIRE_OPERATOR_REVIEW")
    if not health:
        flags.append("MISSING_HEALTH_REFRESH")
    elif paper_health.get("status") not in {None, "HEALTHY"}:
        flags.append("PAPER_HEALTH_NOT_HEALTHY")
    if not handoff:
        flags.append("MISSING_MORNING_HANDOFF")
    elif handoff.get("status") != "READY_FOR_MORNING_OPERATOR_REVIEW":
        flags.append("MORNING_HANDOFF_NOT_READY")
    if not guard:
        flags.append("MISSING_APPROVAL_SAFETY_GUARD")
    elif guard.get("guard_status") != "PASS_REPORT_ONLY":
        flags.append("APPROVAL_SAFETY_GUARD_NOT_PASSING")
    return flags


def _render_phase3an_overnight_refresh_continuity_markdown(
    payload: dict[str, Any],
) -> str:
    summary = _dict(payload.get("summary"))
    flags = list(payload.get("continuity_flags") or [])
    safe_commands = list(payload.get("safe_overnight_commands") or [])
    blocked = list(payload.get("blocked_without_human_approval") or [])
    lines = [
        "# Phase 3AN-R8 Overnight Refresh Continuity",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Status: `{payload.get('status')}`",
        f"- First blocker: `{summary.get('first_blocker')}`",
        f"- R5: `{summary.get('r5_status')}` cycle `{summary.get('r5_cycle_number')}/{summary.get('r5_total_cycles')}`",
        f"- Positive EV rows: `{summary.get('positive_ev_rows')}`",
        f"- Paper-ready candidates: `{summary.get('paper_ready_candidates')}`",
        f"- Market refresh: `{summary.get('market_refresh_status')}`",
        f"- Markets/snapshots/forecasts: `{summary.get('markets_seen')}/{summary.get('snapshots_inserted')}/{summary.get('forecasts_inserted')}`",
        f"- Paper health: `{summary.get('paper_health_status')}`",
        f"- Eligible exact settlements: `{summary.get('eligible_exact_settlements')}`",
        f"- Morning handoff: `{summary.get('handoff_status')}`",
        f"- Approval guard: `{summary.get('approval_guard_status')}`",
        "",
        "## Safety",
        "",
        "- Report only.",
        "- No live/demo exchange execution.",
        "- No order submit/cancel/replace.",
        "- No paper trade creation.",
        "- No automatic apply.",
        "",
        "## Continuity Flags",
        "",
    ]
    lines.extend(f"- `{flag}`" for flag in flags) if flags else lines.append("- None")
    lines.extend(["", "## Safe Overnight Commands", ""])
    lines.extend(f"{index}. `{command}`" for index, command in enumerate(safe_commands, start=1))
    lines.extend(["", "## Blocked Without Human Approval", ""])
    lines.extend(f"- `{command}`" for command in blocked)
    lines.extend(["", f"Next action: {payload.get('exact_next_action')}"])
    return "\n".join(lines) + "\n"


def _phase3an_session_db_url(session: Session) -> str | None:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    return str(url) if url is not None else None


def _timestamp_for_path() -> str:
    return utc_now().strftime("%Y%m%d_%H%M%S")


def _phase3az_before_after(*, reports_dir: Path) -> dict[str, Any]:
    from kalshi_predictor.phase3az import write_phase3az_gap_analysis_report

    before = _load_json(reports_dir / "phase3az" / "phase3az_gap_analysis.json")
    error = None
    try:
        artifacts = write_phase3az_gap_analysis_report(
            output_dir=reports_dir / "phase3az",
            reports_dir=reports_dir,
        )
        after = _load_json(artifacts.json_path)
    except Exception as exc:  # noqa: BLE001 - gap fix report must still terminate.
        after = {}
        error = str(exc)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "mode": "PAPER_READ_ONLY_PHASE3AZ_BEFORE_AFTER",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "before_summary": _dict(before.get("summary")),
        "after_summary": _dict(after.get("summary")),
        "before_gap_count": len(before.get("gaps", [])) if isinstance(before.get("gaps"), list) else 0,
        "after_gap_count": len(after.get("gaps", [])) if isinstance(after.get("gaps"), list) else 0,
        "rerun_error": error,
        "source_reports": {
            "before": str(reports_dir / "phase3az" / "phase3az_gap_analysis.json"),
            "after": str(reports_dir / "phase3az" / "phase3az_gap_analysis.json"),
        },
    }


def _phase3an_dashboard_status(
    *,
    crypto: dict[str, Any],
    paper: dict[str, Any],
    settlement: dict[str, Any],
    burndown: dict[str, Any],
    general: dict[str, Any],
    sports: dict[str, Any],
    economic: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AN",
        "phase_version": PHASE_3AN_OPERATIONAL_VERSION,
        "summary": {
            "crypto_watch": {
                "status": crypto.get("classification"),
                "slow_stage": crypto.get("slowest_stage"),
                "heartbeat": crypto.get("last_heartbeat"),
                "paper_ready_rows": _dict(crypto.get("summary")).get("paper_ready_rows"),
                "next_action": crypto.get("exact_next_action"),
            },
            "paper_funnel": {
                "status": _dict(paper.get("summary")).get("status"),
                "first_hard_blocker": _dict(paper.get("summary")).get("first_hard_blocker"),
                "top_reason": (paper.get("top_block_reasons") or [["NONE", 0]])[0],
                "tradeable_rows": _dict(paper.get("summary")).get("tradeable_rows"),
            },
            "settlement": {
                "status": _dict(settlement.get("summary")).get("status"),
                "exact_eligible_trades": _dict(settlement.get("summary")).get("exact_eligible_trades"),
                "apply_command_exposed": _dict(settlement.get("summary")).get("apply_command_exposed"),
            },
            "general_sources": {
                "USDA": _dict(_dict(general.get("sources")).get("USDA")).get("status"),
                "Cushman": _dict(_dict(general.get("sources")).get("Cushman")).get("status"),
                "FlightAware": _dict(_dict(general.get("sources")).get("FlightAware")).get("status"),
                "source_evidence_ready_rows": _dict(general.get("summary")).get("source_evidence_ready_rows"),
                "source_evidence_status": _dict(general.get("summary")).get("source_evidence_status"),
                "first_hard_blocker": _dict(general.get("summary")).get("first_hard_blocker"),
                "link_safe_rows": _dict(general.get("summary")).get("link_safe_rows"),
                "forecast_safe_rows": _dict(general.get("summary")).get("forecast_safe_rows"),
                "date_stable_rows": _dict(general.get("summary")).get("date_stable_rows"),
                "date_stable_missing_rows": _dict(general.get("summary")).get("date_stable_missing_rows"),
                "review_gated_rows": _dict(general.get("summary")).get("review_gated_rows"),
                "blocked_rows": _dict(general.get("summary")).get("blocked_rows"),
                "proprietary_blocked_rows": _dict(general.get("summary")).get("proprietary_blocked_rows"),
                "wrong_date_rows": _dict(general.get("summary")).get("wrong_date_rows"),
                "next_action": general.get("exact_next_action"),
            },
            "phase3bb_r2": {
                "evidence_ready_rows": _dict(burndown.get("summary")).get("evidence_ready_rows"),
                "source_blocker": burndown.get("exact_next_action"),
            },
            "sports": {
                "placeholder_rows": _dict(sports.get("summary")).get("placeholder_rows"),
                "partial_provenance_markets": _dict(sports.get("summary")).get("partial_provenance_markets"),
                "reason_codes": sports.get("reason_codes", []),
            },
            "economic_news": {
                "blocker_reason": _dict(economic.get("summary")).get("blocker_reason"),
                "economic_compatible_parsed_markets": _dict(economic.get("summary")).get(
                    "economic_compatible_parsed_markets"
                ),
                "news_compatible_parsed_markets": _dict(economic.get("summary")).get(
                    "news_compatible_parsed_markets"
                ),
                "economic_current_parsed_markets": _dict(economic.get("summary")).get(
                    "economic_current_parsed_markets"
                ),
                "news_current_parsed_markets": _dict(economic.get("summary")).get(
                    "news_current_parsed_markets"
                ),
                "economic_exact_linked_current_markets": _dict(economic.get("summary")).get(
                    "economic_exact_linked_current_markets"
                ),
                "news_exact_linked_current_markets": _dict(economic.get("summary")).get(
                    "news_exact_linked_current_markets"
                ),
                "economic_exact_linked_current_without_parsed_leg": _dict(
                    economic.get("summary")
                ).get("economic_exact_linked_current_without_parsed_leg"),
                "news_exact_linked_current_without_parsed_leg": _dict(
                    economic.get("summary")
                ).get("news_exact_linked_current_without_parsed_leg"),
                "economic_current_handoff_blocker": _dict(economic.get("summary")).get(
                    "economic_current_handoff_blocker"
                ),
                "news_current_handoff_blocker": _dict(economic.get("summary")).get(
                    "news_current_handoff_blocker"
                ),
            },
        },
    }


def _render_phase3an_executive_summary(
    *,
    crypto: dict[str, Any],
    paper: dict[str, Any],
    settlement: dict[str, Any],
    burndown: dict[str, Any],
    general: dict[str, Any],
    sports: dict[str, Any],
    economic: dict[str, Any],
    phase3az: dict[str, Any],
) -> str:
    general_sources = _dict(general.get("sources"))
    lines = [
        "# Phase 3AN Executive Summary",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Crypto stuck or slow: `{crypto.get('classification')}`",
        f"- Slow stage: `{crypto.get('slowest_stage')}`",
        (
            "- Paper funnel: "
            f"`{_dict(paper.get('summary')).get('rankings_reviewed')}` rankings, "
            f"`{_dict(paper.get('summary')).get('tradeable_rows')}` tradeable, "
            f"first blocker `{_dict(paper.get('summary')).get('first_hard_blocker')}`"
        ),
        f"- No-trade correct now: `{_dict(paper.get('summary')).get('no_trade_correct_now')}`",
        f"- Settlement healthy: `{_dict(settlement.get('summary')).get('status')}`",
        f"- Settlement apply needed: `{_dict(settlement.get('summary')).get('settlement_apply_needed')}`",
        f"- USDA blocker: `{_dict(general_sources.get('USDA')).get('status')}`",
        f"- Cushman blocker: `{_dict(general_sources.get('Cushman')).get('status')}`",
        f"- FlightAware blocker: `{_dict(general_sources.get('FlightAware')).get('status')}`",
        f"- Sports blocker: `{', '.join(sports.get('reason_codes', []))}`",
        f"- Economic/news blocker: `{_dict(economic.get('summary')).get('blocker_reason')}`",
        (
            "- Economic/news current handoff: "
            f"economic `{_dict(economic.get('summary')).get('economic_current_handoff_blocker')}`, "
            f"news `{_dict(economic.get('summary')).get('news_current_handoff_blocker')}`"
        ),
        f"- Next single best operator action: {_next_single_operator_action(crypto, general)}",
        f"- Code changes required: `{_code_change_required(burndown, sports, economic)}`",
        f"- External source values required: `{_external_source_values_required(general)}`",
        (
            "- Safety protections working: paper/read-only reports, no live/demo orders, "
            "no forced settlements, no source promotion, no threshold lowering."
        ),
        f"- Phase 3AZ before gaps: `{phase3az.get('before_gap_count')}`",
        f"- Phase 3AZ after gaps: `{phase3az.get('after_gap_count')}`",
        "",
    ]
    return "\n".join(lines)


def _render_phase3an_next_actions(
    *,
    crypto: dict[str, Any],
    general: dict[str, Any],
    sports: dict[str, Any],
    economic: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "# Phase 3AN Next Actions",
            "",
            "## P0",
            "",
            "- Keep all paths paper/read-only. Do not run settlement apply from Phase 3AN.",
            "",
            "## P1",
            "",
            f"- Crypto watcher: {crypto.get('exact_next_action')}",
            f"- USDA exact source: {_dict(_dict(general.get('sources')).get('USDA')).get('next_action')}",
            "",
            "## P2",
            "",
            f"- Cushman: {_dict(_dict(general.get('sources')).get('Cushman')).get('next_action')}",
            f"- FlightAware: {_dict(_dict(general.get('sources')).get('FlightAware')).get('next_action')}",
            f"- Sports evidence: {sports.get('exact_next_action')}",
            "",
            "## P3",
            "",
            f"- Economic/news: {economic.get('exact_next_action')}",
            (
                "- Economic/news parser backfill plan: "
                "`kalshi-bot phase3an-economic-news-parser-backfill-plan "
                "--output-dir reports/phase3an --limit 500`."
            ),
            (
                "- Economic link-event repair plan: "
                "`kalshi-bot phase3an-economic-link-event-repair-plan "
                "--output-dir reports/phase3an --limit 500`."
            ),
            (
                "- Economic link-event repair dry-run: "
                "`kalshi-bot phase3an-economic-link-event-repair "
                "--output-dir reports/phase3an --limit 500 --max-records 50`."
            ),
            (
                "- Economic parser-leg backfill dry-run: "
                "`kalshi-bot phase3an-economic-parser-leg-backfill "
                "--output-dir reports/phase3an --limit 500 --max-records 50`."
            ),
            (
                "- Economic operator approval packet: "
                "`kalshi-bot phase3an-economic-operator-approval-packet "
                "--output-dir reports/phase3an --limit 500 --max-records 50`."
            ),
            (
                "- Economic approval safety guard: "
                "`kalshi-bot phase3an-economic-approval-safety-guard "
                "--output-dir reports/phase3an "
                "--packet-path reports/phase3an/economic_operator_approval_packet.json`."
            ),
            (
                "- Morning operator handoff: "
                "`kalshi-bot phase3an-economic-morning-operator-handoff "
                "--output-dir reports/phase3an --reports-dir reports`."
            ),
            (
                "- Overnight refresh continuity: "
                "`kalshi-bot phase3an-overnight-refresh-continuity "
                "--output-dir reports/phase3an --reports-dir reports`."
            ),
            "",
            "## P4",
            "",
            "- Rerun `kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports`.",
            "",
        ]
    )


def _next_single_operator_action(crypto: dict[str, Any], general: dict[str, Any]) -> str:
    if crypto.get("classification") in {"RUNNING_CYCLE_OVERDUE", "WATCHER_STALE", "RESTART_SAFE"}:
        return "Review `reports/phase3an/crypto_watch_doctor.json`, then run the dry-run restart plan."
    return _phase3an_source_next_action(general)


def _code_change_required(
    burndown: dict[str, Any],
    sports: dict[str, Any],
    economic: dict[str, Any],
) -> bool:
    source_ready = _intish(_dict(burndown.get("summary")).get("evidence_ready_rows")) > 0
    sports_safe = _intish(_dict(sports.get("summary")).get("safe_repair_rows")) > 0
    econ_active = (
        _intish(_dict(economic.get("summary")).get("economic_current_parsed_markets"))
        + _intish(_dict(economic.get("summary")).get("news_current_parsed_markets"))
        > 0
    )
    return source_ready or sports_safe or econ_active


def _external_source_values_required(general: dict[str, Any]) -> bool:
    sources = _dict(general.get("sources"))
    return any(
        _dict(sources.get(name)).get("status")
        in {
            "USDA_DATE_MISMATCH",
            "USDA_EXACT_REPORT_NOT_FOUND",
            "USDA_VALUES_UNAVAILABLE",
            "CUSHMAN_VALUES_UNAVAILABLE",
            "FLIGHTAWARE_READY_FOR_REVIEW",
        }
        for name in ("USDA", "Cushman", "FlightAware")
    )


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines: list[str] = []
    for file_path in files:
        if not file_path.exists() or file_path == path:
            continue
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.as_posix()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
