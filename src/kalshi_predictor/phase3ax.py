from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    Forecast,
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
)
from kalshi_predictor.market_legs import MarketLegParseResult, parse_and_store_market_legs
from kalshi_predictor.phase3au import (
    DEFAULT_HEARTBEAT_DIR,
    LongJobHeartbeat,
    deadline_reached,
    stop_after_deadline,
)
from kalshi_predictor.phase3aw import (
    COMMANDS_NOT_REGISTERED,
    CONFLICTS_WITH_R5,
    CURRENT_AND_TRUSTED,
    CURRENT_BUT_DIAGNOSTIC_ONLY,
    EV_NOT_POSITIVE,
    FORECAST_STALE,
    MISSING,
    PAPER_READY_CANDIDATE_AVAILABLE,
    RANKING_GAP,
    SNAPSHOT_STALE,
    STALE_ARTIFACT,
    WATCHER_NOT_RUNNING_OR_STALE,
    build_phase3aw_dashboard_truth,
)
from kalshi_predictor.sports.derived_schedule import (
    SportsDerivedScheduleSummary,
    derive_sports_schedule_from_market_legs,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

DEFAULT_GAP_OUTPUT_DIR = Path("reports/phase3ax")
PHASE3AX_GAP_VERSION = "phase3ax_app_gap_analysis_v1"
CRYPTO_SERIES_TICKERS = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
RECENT_REPORT_COMMANDS = (
    "phase3at-handoff-report",
    "phase3ar-url-audit",
    "phase3ar-link-repair-report",
    "phase3ar-refresh-catalog-for-opportunities",
)
REPORT_AUDIT_TARGETS = (
    Path("phase3bc_r3/phase3bc_r3_active_crypto_refresh.json"),
    Path("phase3bc_r5/phase3bc_r5_status.json"),
    Path("phase3bc_r5/phase3bc_r5_crypto_freshness_watch.json"),
    Path("phase3bc_r7/phase3bc_r7_crypto_ranking_coverage_repair.json"),
    Path("phase3at/phase3at_active_router.json"),
    Path("phase3at/forecast_ranking_diagnostic.json"),
    Path("phase3at/opportunity_funnel.json"),
    Path("phase3ar/phase3ar_crypto_forecast_coverage.json"),
    Path("phase3ar/paper_ready_gate_after_url_repair.json"),
    Path("phase3ar/url_audit.json"),
    Path("phase3ar/catalog_refresh_plan.json"),
    Path("phase3bc/phase3bc_crypto_clean_opportunity_router.json"),
    Path("phase3bb_r2_sources/source_readiness_matrix.json"),
    Path("phase3bb_r3_source_activation/source_evidence_activation.json"),
    Path("phase3bb_r4_flightaware/flightaware_review_link_gate.json"),
    Path("phase3bb_r5_flightaware/flightaware_date_stable_evidence.json"),
    Path("phase3an/general_sources_status.json"),
    Path("phase3an/sports_blocker_report.json"),
    Path("phase3an/economic_news_watch.json"),
    Path("phase3ay/positive_ev_acceleration.json"),
    Path("phase3ah_r3/sports_provenance_repair.json"),
    Path("phase3ax_r9/guarded_refresh_job.json"),
    Path("phase3az/phase3az_gap_analysis.json"),
    Path("phase3aw/dashboard_truth.json"),
)


@dataclass(frozen=True)
class Phase3AXGapAnalysisArtifacts:
    output_dir: Path
    executive_summary_path: Path
    next_codex_task_path: Path
    next_operator_commands_path: Path
    app_gap_analysis_json_path: Path
    app_gap_analysis_markdown_path: Path
    report_freshness_audit_path: Path
    command_registry_audit_path: Path
    crypto_pipeline_truth_path: Path
    source_evidence_gap_status_path: Path
    sports_gap_status_path: Path
    economic_news_gap_status_path: Path
    ui_dashboard_truth_status_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class SportsDerivationLongRunResult:
    summary: SportsDerivedScheduleSummary
    parse_result: MarketLegParseResult | None
    heartbeat_path: str
    checkpoint_path: str
    stopped_early: bool
    commits_created: int
    resume: bool


def run_resumable_sports_derivation(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    build_features: bool = True,
    refresh_features: bool = False,
    parse_first: bool = False,
    refresh_parse: bool = False,
    resume: bool = False,
    heartbeat_dir: Path | None = None,
    progress_every: int = 100,
    checkpoint_every: int = 100,
    stop_after_minutes: int | None = None,
    commit_every: int = 100,
) -> SportsDerivationLongRunResult:
    """Run sports derivation with Phase 3AU heartbeat, checkpoint, and stop guards."""
    resolved = settings or get_settings()
    heartbeat = LongJobHeartbeat(
        "derive-sports-schedule",
        output_dir=heartbeat_dir or DEFAULT_HEARTBEAT_DIR,
        checkpoint_every=checkpoint_every,
    )
    deadline = stop_after_deadline(stop_after_minutes)
    commits_created = 0
    last_commit_processed = 0

    heartbeat.emit(
        stage="SPORTS_DERIVE_START",
        message="Starting Phase 3AX safe sports derivation.",
        force_checkpoint=True,
        extra={
            "limit": limit,
            "build_features": build_features,
            "refresh_features": refresh_features,
            "parse_first": parse_first,
            "refresh_parse": refresh_parse,
            "resume": resume,
            "stop_after_minutes": stop_after_minutes,
            "commit_every": commit_every,
        },
    )

    parse_result: MarketLegParseResult | None = None
    if parse_first and not deadline_reached(deadline):
        heartbeat.emit(
            stage="SPORTS_PARSE_START",
            message="Parsing market legs before sports derivation.",
            force_checkpoint=True,
        )
        parse_result = parse_and_store_market_legs(
            session,
            limit=limit,
            refresh=refresh_parse,
        )
        session.commit()
        commits_created += 1
        heartbeat.emit(
            stage="SPORTS_PARSE_COMPLETE",
            processed=parse_result.markets_scanned,
            total=parse_result.markets_scanned,
            message=f"Parsed {parse_result.legs_inserted} sports/market leg rows.",
            force_checkpoint=True,
            extra={
                "legs_inserted": parse_result.legs_inserted,
                "markets_with_legs": parse_result.markets_with_legs,
                "skipped_existing": parse_result.markets_skipped_existing,
            },
        )

    def progress(event: dict[str, object]) -> None:
        nonlocal commits_created, last_commit_processed
        processed = int(event.get("processed") or 0)
        if (
            commit_every > 0
            and processed > 0
            and processed % commit_every == 0
            and processed != last_commit_processed
        ):
            session.commit()
            commits_created += 1
            last_commit_processed = processed
        heartbeat.emit(
            stage="SPORTS_DERIVE",
            processed=processed,
            total=int(event.get("total") or 0),
            current_item=str(event.get("ticker") or ""),
            message=str(event.get("status") or "Progress"),
            extra=event,
        )

    def should_stop() -> bool:
        return deadline_reached(deadline)

    summary = SportsDerivedScheduleSummary(0, 0, 0, 0, 0, 0, 0, 0, 0)
    stopped_early = False
    if should_stop():
        stopped_early = True
    else:
        summary = derive_sports_schedule_from_market_legs(
            session,
            limit=limit,
            build_features=build_features,
            refresh_features=refresh_features,
            settings=resolved,
            progress_callback=progress,
            progress_every=progress_every,
            should_stop=should_stop,
        )
        stopped_early = summary.stopped_early

    heartbeat.emit(
        stage="STOPPED_EARLY" if stopped_early else "COMPLETE",
        processed=summary.sports_markets_seen,
        total=summary.markets_scanned,
        message=(
            "Stopped early by --stop-after-minutes; rerun with --resume."
            if stopped_early
            else "Sports derivation completed."
        ),
        force_checkpoint=True,
        extra={
            "teams_created": summary.teams_created,
            "games_created": summary.games_created,
            "links_created": summary.links_created,
            "links_existing": summary.links_existing,
            "features_created": summary.features_created,
            "features_existing": summary.features_existing,
            "commits_created": commits_created,
        },
    )

    return SportsDerivationLongRunResult(
        summary=summary,
        parse_result=parse_result,
        heartbeat_path=str(heartbeat.heartbeat_path),
        checkpoint_path=str(heartbeat.checkpoint_path),
        stopped_early=stopped_early,
        commits_created=commits_created,
        resume=resume,
    )


def build_phase3ax_gap_analysis(
    session: Session,
    *,
    output_dir: Path = DEFAULT_GAP_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    registered_commands: set[str] | None = None,
    stale_after_minutes: int = 120,
    db_writer_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a report-only app gap analysis from current DB and trusted reports."""
    resolved = settings or get_settings()
    generated_at = utc_now()
    commands = set(registered_commands or ())
    commands.add("phase3ax-gap-analysis")
    phase3aw_truth = build_phase3aw_dashboard_truth(
        session,
        output_dir=Path("reports/phase3aw"),
        reports_dir=reports_dir,
        settings=resolved,
        command_args=[
            "kalshi-bot",
            "phase3aw-dashboard-truth",
            "--output-dir",
            "reports/phase3aw",
            "--reports-dir",
            str(reports_dir),
        ],
        stale_after_minutes=stale_after_minutes,
    )
    runtime = _runtime_identity(
        session,
        settings=resolved,
        command_args=command_args or [],
        generated_at=generated_at,
        db_writer_status=db_writer_status,
        phase3aw_truth=phase3aw_truth,
    )
    command_audit = _command_registry_audit(
        reports_dir=reports_dir,
        registered_commands=commands,
    )
    report_audit = _report_freshness_audit(
        reports_dir=reports_dir,
        generated_at=generated_at,
        registered_commands=commands,
        command_audit=command_audit,
        phase3aw_truth=phase3aw_truth,
        stale_after_minutes=stale_after_minutes,
    )
    crypto_truth = _crypto_pipeline_truth(
        session,
        phase3aw_truth=phase3aw_truth,
        reports_dir=reports_dir,
    )
    source_status = _source_evidence_gap_status(reports_dir)
    sports_status = _sports_gap_status(session, reports_dir=reports_dir)
    economic_news_status = _economic_news_gap_status(reports_dir)
    guarded_refresh_status = _guarded_refresh_job_status(reports_dir)
    dashboard_status = _ui_dashboard_truth_status(
        phase3aw_truth=phase3aw_truth,
        report_audit=report_audit,
    )
    next_task = _select_next_codex_task(
        crypto_truth=crypto_truth,
        command_audit=command_audit,
        dashboard_status=dashboard_status,
        source_status=source_status,
        sports_status=sports_status,
        economic_news_status=economic_news_status,
        guarded_refresh_status=guarded_refresh_status,
    )
    next_operator = _next_operator_commands(
        next_task=next_task,
        registered_commands=commands,
        crypto_truth=crypto_truth,
    )
    summary = {
        "app_state": _app_state(crypto_truth, dashboard_status),
        "true_current_blocker": crypto_truth["true_current_blocker"],
        "current_positive_ev_rows": crypto_truth["current_positive_ev_rows"],
        "paper_ready_candidates": crypto_truth["paper_ready_candidates"],
        "stale_or_conflicting_report_count": report_audit["stale_or_conflicting_count"],
        "missing_command_count": len(command_audit["missing_commands"]),
        "selected_next_codex_task": next_task["task_phase_name"],
        "next_operator_command": next_operator["run_now"],
    }
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AX",
        "phase_version": PHASE3AX_GAP_VERSION,
        "mode": "PAPER_ONLY_APP_GAP_ANALYSIS",
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "runtime_identity": runtime,
        "summary": summary,
        "crypto_pipeline_truth": crypto_truth,
        "report_freshness_audit": report_audit,
        "command_registry_audit": command_audit,
        "source_evidence_gap_status": source_status,
        "sports_gap_status": sports_status,
        "economic_news_gap_status": economic_news_status,
        "guarded_refresh_job_status": guarded_refresh_status,
        "ui_dashboard_truth_status": dashboard_status,
        "next_codex_task": next_task,
        "next_operator_commands": next_operator,
        "operator_do_not_run": [
            "Do not force paper trades.",
            (
                "Do not lower EV, score, confidence, liquidity, spread, "
                "settlement, source-readiness, or risk thresholds."
            ),
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not treat stale Phase 3AR/3AT artifacts as current paper-ready evidence.",
        ],
    }


def write_phase3ax_gap_analysis_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_GAP_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    registered_commands: set[str] | None = None,
    stale_after_minutes: int = 120,
    db_writer_status: dict[str, Any] | None = None,
) -> Phase3AXGapAnalysisArtifacts:
    payload = build_phase3ax_gap_analysis(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        registered_commands=registered_commands,
        stale_after_minutes=stale_after_minutes,
        db_writer_status=db_writer_status,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_codex_task_path = output_dir / "NEXT_CODEX_TASK.md"
    next_operator_commands_path = output_dir / "NEXT_OPERATOR_COMMANDS.md"
    app_gap_analysis_json_path = output_dir / "app_gap_analysis.json"
    app_gap_analysis_markdown_path = output_dir / "app_gap_analysis.md"
    report_freshness_audit_path = output_dir / "report_freshness_audit.json"
    command_registry_audit_path = output_dir / "command_registry_audit.json"
    crypto_pipeline_truth_path = output_dir / "crypto_pipeline_truth.json"
    source_evidence_gap_status_path = output_dir / "source_evidence_gap_status.json"
    sports_gap_status_path = output_dir / "sports_gap_status.json"
    economic_news_gap_status_path = output_dir / "economic_news_gap_status.json"
    ui_dashboard_truth_status_path = output_dir / "ui_dashboard_truth_status.json"
    manifest_path = output_dir / "MANIFEST.sha256"

    _write_json(app_gap_analysis_json_path, payload)
    _write_json(report_freshness_audit_path, payload["report_freshness_audit"])
    _write_json(command_registry_audit_path, payload["command_registry_audit"])
    _write_json(crypto_pipeline_truth_path, payload["crypto_pipeline_truth"])
    _write_json(source_evidence_gap_status_path, payload["source_evidence_gap_status"])
    _write_json(sports_gap_status_path, payload["sports_gap_status"])
    _write_json(economic_news_gap_status_path, payload["economic_news_gap_status"])
    _write_json(ui_dashboard_truth_status_path, payload["ui_dashboard_truth_status"])
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_codex_task_path.write_text(_render_next_codex_task(payload), encoding="utf-8")
    next_operator_commands_path.write_text(
        _render_next_operator_commands(payload),
        encoding="utf-8",
    )
    app_gap_analysis_markdown_path.write_text(
        _render_app_gap_analysis_markdown(payload),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            next_codex_task_path,
            next_operator_commands_path,
            app_gap_analysis_json_path,
            app_gap_analysis_markdown_path,
            report_freshness_audit_path,
            command_registry_audit_path,
            crypto_pipeline_truth_path,
            source_evidence_gap_status_path,
            sports_gap_status_path,
            economic_news_gap_status_path,
            ui_dashboard_truth_status_path,
        ],
    )
    return Phase3AXGapAnalysisArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        next_codex_task_path=next_codex_task_path,
        next_operator_commands_path=next_operator_commands_path,
        app_gap_analysis_json_path=app_gap_analysis_json_path,
        app_gap_analysis_markdown_path=app_gap_analysis_markdown_path,
        report_freshness_audit_path=report_freshness_audit_path,
        command_registry_audit_path=command_registry_audit_path,
        crypto_pipeline_truth_path=crypto_pipeline_truth_path,
        source_evidence_gap_status_path=source_evidence_gap_status_path,
        sports_gap_status_path=sports_gap_status_path,
        economic_news_gap_status_path=economic_news_gap_status_path,
        ui_dashboard_truth_status_path=ui_dashboard_truth_status_path,
        manifest_path=manifest_path,
    )


def _runtime_identity(
    session: Session,
    *,
    settings: Settings,
    command_args: list[str],
    generated_at: Any,
    db_writer_status: dict[str, Any] | None,
    phase3aw_truth: dict[str, Any],
) -> dict[str, Any]:
    bind = session.get_bind()
    db_url = _database_url(session)
    sqlite_path = _sqlite_file_path(bind)
    db_fingerprint = _db_fingerprint(db_url, sqlite_path)
    repo_root = _repo_root()
    writer_status = db_writer_status if db_writer_status is not None else db_writer_monitor()
    integrity = _sqlite_integrity_check(session, sqlite_path)
    migration_revision = _migration_revision(session)
    return {
        "generated_at": generated_at.isoformat(),
        "repository_root": str(repo_root or Path.cwd()),
        "git_branch": _git_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root),
        "git_commit": _git_output(["git", "rev-parse", "--short", "HEAD"], repo_root),
        "git_dirty_status": _git_dirty_status(repo_root),
        "python_executable": sys.executable,
        "installed_package_path": str(Path(__file__).resolve().parent),
        "database_url_redacted": db_url,
        "sqlite_file_path": str(sqlite_path) if sqlite_path is not None else None,
        "db_fingerprint": db_fingerprint,
        "migration_revision": migration_revision,
        "sqlite_integrity_check": integrity,
        "active_db_writer_status": writer_status,
        "current_timezone": os.environ.get("TZ") or "system-local",
        "command_args": command_args,
        "data_watermark": phase3aw_truth.get("metadata", {}).get("data_watermark", {}),
        "safety_flags": {
            "paper_only": True,
            "live_demo_execution_blocked": True,
            "order_submission_cancel_replace_blocked": True,
            "paper_trade_creation_blocked": True,
            "thresholds_lowered": False,
        },
        "runtime_identity_state": _runtime_state(
            repo_root,
            sqlite_path,
            integrity,
            migration_revision,
        ),
        "settings": {
            "kalshi_env": settings.kalshi_env,
            "db_backend": settings.db_backend,
            "opportunity_min_edge": str(settings.opportunity_min_edge),
            "opportunity_min_score": str(settings.opportunity_min_score),
            "opportunity_max_spread": str(settings.opportunity_max_spread),
            "opportunity_min_liquidity": str(settings.opportunity_min_liquidity),
        },
    }


def _command_registry_audit(
    *,
    reports_dir: Path,
    registered_commands: set[str],
) -> dict[str, Any]:
    references = _command_references_from_reports(reports_dir)
    for command in RECENT_REPORT_COMMANDS:
        references.setdefault(command, [])
    rows = []
    for command, sources in sorted(references.items()):
        registered = command in registered_commands
        rows.append(
            {
                "command": command,
                "registered": registered,
                "sources": sorted(set(sources)),
                "equivalent_registered_command": _equivalent_command(command, registered_commands),
                "recommended_fix": (
                    "No action."
                    if registered
                    else _missing_command_fix(command, registered_commands)
                ),
            }
        )
    missing = [row for row in rows if not row["registered"]]
    return {
        "registered_command_count": len(registered_commands),
        "referenced_command_count": len(rows),
        "rows": rows,
        "missing_commands": missing,
        "missing_command_names": [row["command"] for row in missing],
        "next_actions_reference_only_registered_commands": True,
    }


def _report_freshness_audit(
    *,
    reports_dir: Path,
    generated_at: Any,
    registered_commands: set[str],
    command_audit: dict[str, Any],
    phase3aw_truth: dict[str, Any],
    stale_after_minutes: int,
) -> dict[str, Any]:
    r5_cycle_at = parse_datetime(
        phase3aw_truth.get("summary", {}).get("r5_latest_report_generated_at")
    )
    missing_by_source: dict[str, list[str]] = {}
    for row in command_audit["missing_commands"]:
        for source in row.get("sources", []):
            missing_by_source.setdefault(source, []).append(row["command"])
    rows = [
        _classify_report_artifact(
            reports_dir=reports_dir,
            relative_path=relative_path,
            generated_at=generated_at,
            r5_cycle_at=r5_cycle_at,
            phase3aw_truth=phase3aw_truth,
            missing_commands=missing_by_source,
            stale_after_minutes=stale_after_minutes,
        )
        for relative_path in REPORT_AUDIT_TARGETS
    ]
    counts = _classification_counts(rows)
    return {
        "generated_at": generated_at.isoformat(),
        "stale_after_minutes": stale_after_minutes,
        "truth_priority": [
            "DB direct current-state query",
            "latest R5 guarded watch status",
            "latest R3 bounded refresh",
            "latest R7 ranking coverage",
            "latest active router",
            "older diagnostic artifacts",
            "markdown summaries",
        ],
        "rows": rows,
        "classification_counts": counts,
        "stale_or_conflicting_count": sum(
            counts.get(key, 0)
            for key in (STALE_ARTIFACT, CONFLICTS_WITH_R5, COMMANDS_NOT_REGISTERED)
        ),
    }


def _crypto_pipeline_truth(
    session: Session,
    *,
    phase3aw_truth: dict[str, Any],
    reports_dir: Path,
) -> dict[str, Any]:
    dashboard_summary = phase3aw_truth.get("summary", {})
    funnel = phase3aw_truth.get("current_crypto_funnel", {})
    r5_summary = _latest_r5_summary_from_reports(reports_dir)
    db_counts = _crypto_db_counts(session)
    true_blocker = str(
        dashboard_summary.get("true_current_blocker") or "UNKNOWN_REQUIRES_INVESTIGATION"
    )
    r5_positive = _int_value(r5_summary.get("positive_ev_rows"))
    if true_blocker == EV_NOT_POSITIVE and r5_positive == 0:
        status = "CORRECTLY_WAITING_FOR_POSITIVE_EV"
    elif true_blocker == PAPER_READY_CANDIDATE_AVAILABLE:
        status = "PAPER_READY_CANDIDATE_AVAILABLE_READ_ONLY"
    else:
        status = "NEEDS_IMPLEMENTATION_OR_OPERATOR_REVIEW"
    return {
        "status": status,
        "true_current_blocker": true_blocker,
        "watch_state": funnel.get("watch_state") or r5_summary.get("watch_state") or "UNKNOWN",
        "phase3bc_main_blocker": funnel.get("phase3bc_main_blocker")
        or r5_summary.get("phase3bc_main_blocker")
        or "UNKNOWN",
        "db_current_active_pure_crypto_markets": db_counts["current_active_pure_crypto_markets"],
        "current_active_pure_crypto_markets": max(
            db_counts["current_active_pure_crypto_markets"],
            _int_value(funnel.get("current_active_crypto_markets")),
        ),
        "current_markets_with_fresh_snapshots": max(
            db_counts["current_markets_with_fresh_snapshots"],
            _int_value(funnel.get("current_snapshots")),
        ),
        "current_markets_with_fresh_crypto_v2_forecasts": max(
            db_counts["current_markets_with_fresh_crypto_v2_forecasts"],
            _int_value(r5_summary.get("forecast_ready_rows")),
        ),
        "current_markets_with_fresh_rankings": max(
            db_counts["current_markets_with_fresh_rankings"],
            _int_value(r5_summary.get("ranking_ready_rows")),
        ),
        "current_positive_ev_rows": _int_value(dashboard_summary.get("current_positive_ev_rows")),
        "positive_raw_ev_rows": _int_value(dashboard_summary.get("current_positive_ev_rows")),
        "positive_executable_ev_rows": _int_value(
            r5_summary.get("positive_ev_preflight_candidates")
            or r5_summary.get("positive_ev_clean_book_rows")
            or 0
        ),
        "verified_kalshi_link_rows": db_counts["verified_kalshi_link_rows"],
        "executable_book_rows": _int_value(r5_summary.get("positive_ev_clean_book_rows")),
        "liquidity_pass_rows": _int_value(
            r5_summary.get("positive_ev_liquidity_positive_rows")
            or r5_summary.get("clean_execution_rows")
        ),
        "spread_pass_rows": _int_value(r5_summary.get("clean_execution_rows")),
        "risk_ready_rows": _int_value(r5_summary.get("positive_ev_preflight_candidates")),
        "paper_ready_candidates": _int_value(dashboard_summary.get("paper_ready_candidates")),
        "paper_orders_created": db_counts["paper_orders_created"],
        "snapshot_stale_rows": _int_value(funnel.get("snapshot_stale_rows")),
        "forecast_stale_rows": _int_value(funnel.get("forecast_stale_rows")),
        "ranking_gap_after_repair": _int_value(funnel.get("ranking_gap_after_repair")),
        "best_current_expected_value_cents": dashboard_summary.get(
            "best_current_expected_value_cents"
        ),
        "best_ev_gap_to_positive_cents": dashboard_summary.get(
            "best_ev_gap_to_positive_cents"
        ),
        "best_ev_candidate_ticker": dashboard_summary.get("best_ev_candidate_ticker"),
        "db_query_status": db_counts["query_status"],
        "expired_or_historical_rows_excluded": True,
        "stale_reports_allowed_to_drive_current_status": False,
    }


def _source_evidence_gap_status(reports_dir: Path) -> dict[str, Any]:
    flightaware_evidence = _read_json(
        reports_dir / "phase3bb_r5_flightaware" / "flightaware_date_stable_evidence.json"
    )
    flightaware_gate = _read_json(
        reports_dir / "phase3bb_r4_flightaware" / "flightaware_review_link_gate.json"
    )
    activation = _read_json(
        reports_dir / "phase3bb_r3_source_activation" / "source_evidence_activation.json"
    )
    status = _read_json(reports_dir / "phase3an" / "general_sources_status.json")
    burndown = _read_json(reports_dir / "phase3an" / "3bb_r2_burndown.json")
    readiness = _read_json(reports_dir / "phase3bb_r2_sources" / "source_readiness_matrix.json")
    if flightaware_evidence:
        summary = (
            flightaware_evidence.get("summary")
            if isinstance(flightaware_evidence.get("summary"), dict)
            else {}
        )
        general_summary = (
            status.get("summary") if isinstance(status.get("summary"), dict) else {}
        )
        next_task = (
            flightaware_evidence.get("next_codex_task")
            if isinstance(flightaware_evidence.get("next_codex_task"), dict)
            else {}
        )
        accepted_rows = _int_value(summary.get("accepted_date_stable_evidence_rows"))
        affected_rows = _int_value(summary.get("affected_rows"))
        review_gated_rows = _int_value(general_summary.get("review_gated_rows")) or (
            affected_rows
            if summary.get("source_value_available_for_review") and accepted_rows == 0
            else 0
        )
        return {
            "status": "GATED",
            "usda_status": "GATED",
            "cushman_status": "GATED",
            "flightaware_status": summary.get("date_stable_evidence_status") or "NOT_FOUND",
            "evidence_ready_rows": accepted_rows,
            "review_evidence_ready_rows": _int_value(
                general_summary.get("source_evidence_ready_rows")
            ),
            "official_free_source_rows": _int_value(
                general_summary.get("official_free_source_rows")
            ),
            "date_stable_rows": accepted_rows,
            "date_stable_missing_rows": _int_value(
                general_summary.get("date_stable_missing_rows")
            )
            or (affected_rows if accepted_rows == 0 else 0),
            "review_gated_rows": review_gated_rows,
            "blocked_rows": _int_value(general_summary.get("blocked_rows")),
            "proprietary_blocked_rows": _int_value(
                general_summary.get("proprietary_blocked_rows")
            ),
            "wrong_date_rows": _int_value(general_summary.get("wrong_date_rows")),
            "link_safe_rows": _int_value(summary.get("link_safe_rows")),
            "forecast_safe_rows": _int_value(summary.get("forecast_safe_rows")),
            "source_date_mismatch_blockers": True,
            "proprietary_review_blockers": True,
            "activation_readiness": "NOT_READY",
            "first_hard_blocker": summary.get("first_hard_blocker") or "UNKNOWN",
            "source_gap_reported_with_exact_evidence": True,
            "phase3ax_r5_source_activation_complete": True,
            "source_evidence_status": general_summary.get("source_evidence_status")
            or "SOURCE_EVIDENCE_CLASSIFIED_GATED",
            "external_source_access_required": bool(
                summary.get("signup_or_paid_product_likely_required")
            ),
            "next_codex_task_phase_name": next_task.get("task_phase_name"),
            "next_codex_task_reason": next_task.get("reason"),
            "next_codex_task_problem": next_task.get("problem_statement"),
            "next_action": "Use Phase 3BB-R5 FlightAware evidence report for follow-up.",
        }
    if flightaware_gate:
        summary = (
            flightaware_gate.get("summary")
            if isinstance(flightaware_gate.get("summary"), dict)
            else {}
        )
        next_task = (
            flightaware_gate.get("next_codex_task")
            if isinstance(flightaware_gate.get("next_codex_task"), dict)
            else {}
        )
        return {
            "status": "GATED",
            "usda_status": "GATED",
            "cushman_status": "GATED",
            "flightaware_status": summary.get("review_gate_status") or "BLOCKED",
            "evidence_ready_rows": _int_value(summary.get("evidence_ready_rows")),
            "link_safe_rows": _int_value(summary.get("link_safe_rows")),
            "forecast_safe_rows": _int_value(summary.get("forecast_safe_rows")),
            "source_date_mismatch_blockers": True,
            "proprietary_review_blockers": True,
            "activation_readiness": summary.get("activation_readiness") or "NOT_READY",
            "first_hard_blocker": summary.get("first_hard_blocker") or "UNKNOWN",
            "source_gap_reported_with_exact_evidence": True,
            "next_codex_task_phase_name": next_task.get("task_phase_name"),
            "next_codex_task_reason": next_task.get("reason"),
            "next_codex_task_problem": next_task.get("problem_statement"),
            "next_action": "Use Phase 3BB-R4 FlightAware gate report for follow-up.",
        }
    if activation:
        summary = activation.get("summary") if isinstance(activation.get("summary"), dict) else {}
        decisions = (
            activation.get("source_activation_decisions")
            if isinstance(activation.get("source_activation_decisions"), list)
            else []
        )
        next_task = (
            activation.get("next_codex_task")
            if isinstance(activation.get("next_codex_task"), dict)
            else {}
        )
        return {
            "status": summary.get("current_status") or "GATED",
            "usda_status": _source_decision_status(decisions, "USDA"),
            "cushman_status": _source_decision_status(decisions, "Cushman"),
            "flightaware_status": _source_decision_status(decisions, "FlightAware"),
            "evidence_ready_rows": _int_value(summary.get("evidence_ready_rows")),
            "link_safe_rows": _int_value(summary.get("link_safe_rows")),
            "forecast_safe_rows": _int_value(summary.get("forecast_safe_rows")),
            "source_date_mismatch_blockers": bool(
                summary.get("source_date_mismatch_blockers")
            ),
            "proprietary_review_blockers": bool(summary.get("proprietary_review_blockers")),
            "activation_readiness": summary.get("activation_readiness") or "NOT_READY",
            "first_hard_blocker": summary.get("first_hard_blocker") or "UNKNOWN",
            "source_gap_reported_with_exact_evidence": True,
            "next_codex_task_phase_name": next_task.get("task_phase_name"),
            "next_codex_task_reason": next_task.get("reason"),
            "next_codex_task_problem": next_task.get("problem_statement"),
            "next_action": (
                "Use Phase 3BB-R3 source activation report for exact source-gate "
                "follow-up."
            ),
        }
    text = " ".join(
        json.dumps(payload, default=str)[:20000] for payload in (status, burndown, readiness)
    )
    return {
        "status": "GATED",
        "usda_status": _source_keyword_status(text, "USDA"),
        "cushman_status": _source_keyword_status(text, "CUSHMAN"),
        "flightaware_status": _source_keyword_status(text, "FLIGHTAWARE"),
        "evidence_ready_rows": _first_int(
            status,
            burndown,
            readiness,
            keys=("evidence_ready_rows", "evidence_ready"),
        ),
        "link_safe_rows": _first_int(
            status,
            burndown,
            readiness,
            keys=("link_safe_rows", "link_safe"),
        ),
        "forecast_safe_rows": _first_int(
            status,
            burndown,
            readiness,
            keys=("forecast_safe_rows", "forecast_safe"),
        ),
        "source_date_mismatch_blockers": "DATE_MISMATCH" in text.upper() or "USDA" in text.upper(),
        "proprietary_review_blockers": "PROPRIETARY" in text.upper() or "CUSHMAN" in text.upper(),
        "activation_readiness": "NOT_READY",
        "next_action": (
            "Finish 3BB-R2 source evidence gates before promoting general-source markets."
        ),
    }


def _sports_gap_status(session: Session, *, reports_dir: Path) -> dict[str, Any]:
    r6_report = _read_json(reports_dir / "phase3ax" / "phase3ax_gap_analysis.json")
    if r6_report:
        summary = (
            r6_report.get("summary")
            if isinstance(r6_report.get("summary"), dict)
            else {}
        )
        first_blocker = (
            r6_report.get("first_blocker")
            if isinstance(r6_report.get("first_blocker"), dict)
            else {}
        )
        safe_rows = _int_value(summary.get("safe_exact_repair_rows")) or _int_value(
            summary.get("phase3z_rows_safe_to_repair")
        )
        gate = str(summary.get("phase3ax_r6_gate") or "UNKNOWN")
        return {
            "status": gate,
            "placeholder_rows": _int_value(summary.get("placeholder_rows")),
            "partial_provenance_rows": _int_value(summary.get("partial_provenance_rows")),
            "safe_repair_rows": safe_rows,
            "sports_market_links": _safe_count(session, select(func.count(SportsMarketLink.id))),
            "sports_games": _safe_count(session, select(func.count(SportsGame.id))),
            "sports_features": _safe_count(session, select(func.count(SportsFeature.id))),
            "schedule_roster_evidence_state": "PHASE3AX_R6_EXACT_DIAGNOSTIC",
            "implementation_needed": safe_rows > 0,
            "phase3ax_r6_completed": True,
            "phase3ax_r6_gate": gate,
            "diagnostic_only_rows": _int_value(summary.get("diagnostic_only_rows")),
            "safe_exact_repair_rows": safe_rows,
            "first_hard_blocker": first_blocker.get("reason") or "UNKNOWN",
            "phase3ae_gate_status": gate,
            "next_action": r6_report.get("recommended_next_action")
            or "Use Phase 3AX-R6 evidence; do not upgrade sports links without safe rows.",
        }
    r3_report = _read_json(reports_dir / "phase3ah_r3" / "sports_provenance_repair.json")
    if r3_report:
        summary = (
            r3_report.get("summary")
            if isinstance(r3_report.get("summary"), dict)
            else {}
        )
        next_task = (
            r3_report.get("next_codex_task")
            if isinstance(r3_report.get("next_codex_task"), dict)
            else {}
        )
        return {
            "status": summary.get("status") or "GATED",
            "placeholder_rows": _int_value(summary.get("placeholder_blocked_rows")),
            "partial_provenance_rows": _int_value(summary.get("partial_legacy_markets")),
            "safe_repair_rows": _int_value(summary.get("rows_safe_to_repair")),
            "sports_market_links": _safe_count(session, select(func.count(SportsMarketLink.id))),
            "sports_games": _safe_count(session, select(func.count(SportsGame.id))),
            "sports_features": _safe_count(session, select(func.count(SportsFeature.id))),
            "schedule_roster_evidence_state": "PHASE3AH_R3_VERIFIED_DIAGNOSTIC",
            "implementation_needed": bool(summary.get("implementation_needed")),
            "phase3ah_r3_completed": bool(
                summary.get("sports_r3_completed_without_safe_rows")
            ),
            "first_hard_blocker": summary.get("first_hard_blocker") or "UNKNOWN",
            "phase3ae_gate_status": summary.get("phase3ae_gate_status"),
            "next_codex_task_phase_name": next_task.get("task_phase_name"),
            "next_codex_task_reason": next_task.get("reason"),
            "next_codex_task_problem": next_task.get("problem_statement"),
            "next_action": (
                "Use Phase 3AH-R3 evidence; do not upgrade sports links without safe rows."
            ),
        }
    report = _read_json(reports_dir / "phase3an" / "sports_blocker_report.json")
    return {
        "status": "GATED",
        "placeholder_rows": _first_int(report, keys=("placeholder_rows", "placeholders")),
        "partial_provenance_rows": _first_int(
            report,
            keys=("partial_provenance_rows", "partial_provenance_markets"),
        ),
        "safe_repair_rows": _first_int(report, keys=("safe_repair_rows", "safe_rows")),
        "sports_market_links": _safe_count(session, select(func.count(SportsMarketLink.id))),
        "sports_games": _safe_count(session, select(func.count(SportsGame.id))),
        "sports_features": _safe_count(session, select(func.count(SportsFeature.id))),
        "schedule_roster_evidence_state": "PARTIAL_OR_DIAGNOSTIC",
        "implementation_needed": True,
        "next_action": (
            "Do not upgrade placeholders or partial provenance without safe repair rows."
        ),
    }


def _economic_news_gap_status(reports_dir: Path) -> dict[str, Any]:
    report = _read_json(reports_dir / "phase3an" / "economic_news_watch.json")
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    first_hard_blocker = str(
        summary.get("first_hard_blocker")
        or _economic_news_first_hard_blocker_from_report(report)
    )
    status = _economic_news_status_from_blocker(first_hard_blocker)
    return {
        "status": status,
        "first_hard_blocker": first_hard_blocker,
        "compatibility_status": summary.get("compatibility_status") or status,
        "economic_compatible_parsed_markets": _first_int(
            report,
            keys=(
                "economic_compatible_parsed_markets",
                "economic_compatible_active_markets",
                "economic_compatible",
                "economic_compatible_markets",
            ),
        ),
        "news_compatible_parsed_markets": _first_int(
            report,
            keys=(
                "news_compatible_parsed_markets",
                "news_compatible_active_markets",
                "news_compatible",
                "news_compatible_markets",
            ),
        ),
        "economic_current_parsed_markets": _first_int(
            report,
            keys=("economic_current_parsed_markets",),
        ),
        "news_current_parsed_markets": _first_int(
            report,
            keys=("news_current_parsed_markets",),
        ),
        "economic_exact_linked_current_markets": _first_int(
            report,
            keys=("economic_exact_linked_current_markets",),
        ),
        "news_exact_linked_current_markets": _first_int(
            report,
            keys=("news_exact_linked_current_markets",),
        ),
        "economic_exact_linked_current_without_parsed_leg": _first_int(
            report,
            keys=("economic_exact_linked_current_without_parsed_leg",),
        ),
        "news_exact_linked_current_without_parsed_leg": _first_int(
            report,
            keys=("news_exact_linked_current_without_parsed_leg",),
        ),
        "exact_linked_current_without_parsed_leg": _first_int(
            report,
            keys=("exact_linked_current_without_parsed_leg",),
        ),
        "current_parsed_missing_exact_link": _first_int(
            report,
            keys=("current_parsed_missing_exact_link",),
        ),
        "active_news_market_count": _first_int(
            report,
            keys=("active_news_market_count", "active_news_markets"),
        ),
        "context_ready_count": _first_int(report, keys=("context_ready_count", "context_ready")),
        "source_freshness": summary.get("source_freshness") or "UNKNOWN",
        "readiness_source": summary.get("readiness_source"),
        "waiting_is_correct": first_hard_blocker
        in {
            "NO_CURRENT_PARSED_MARKETS",
            "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS",
            "WAITING_FOR_COMPATIBLE_MARKETS",
        },
        "next_action": report.get("exact_next_action")
        or "Wait for compatible parsed markets; do not force links or forecasts.",
        "next_registered_command": summary.get("next_registered_command"),
    }


def _economic_news_first_hard_blocker_from_report(report: dict[str, Any]) -> str:
    handoff = report.get("current_market_handoff")
    domains = _as_dict(handoff).get("domains")
    if not isinstance(domains, dict):
        return "WAITING_FOR_COMPATIBLE_MARKETS"
    blockers = {
        str(_as_dict(domains.get("economic")).get("first_blocker") or ""),
        str(_as_dict(domains.get("news")).get("first_blocker") or ""),
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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _economic_news_status_from_blocker(first_hard_blocker: str) -> str:
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


def _guarded_refresh_job_status(reports_dir: Path) -> dict[str, Any]:
    report = _read_json(reports_dir / "phase3ax_r9" / "guarded_refresh_job.json")
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    status = str(summary.get("status") or "MISSING")
    complete_statuses = {"STARTED", "ALREADY_RUNNING_NO_DUPLICATE_STARTED", "RUNNING"}
    r5_running = bool(summary.get("r5_running"))
    complete = status in complete_statuses and r5_running
    return {
        "status": status,
        "complete": complete,
        "r5_running": r5_running,
        "r5_pid": summary.get("r5_pid"),
        "r5_stale_report": bool(summary.get("r5_stale_report")),
        "r5_latest_age_seconds": summary.get("r5_latest_age_seconds"),
        "r5_freshness_window_minutes": summary.get("r5_freshness_window_minutes"),
        "duplicate_refused": bool(summary.get("duplicate_refused")),
        "dashboard_truth_refreshed": bool(summary.get("dashboard_truth_refreshed")),
        "gap_analysis_refreshed": bool(summary.get("gap_analysis_refreshed")),
        "operator_next_action": report.get("operator_next_action"),
        "next_codex_task_phase_name": (report.get("next_codex_task") or {}).get(
            "task_phase_name"
        ),
    }


def _ui_dashboard_truth_status(
    *,
    phase3aw_truth: dict[str, Any],
    report_audit: dict[str, Any],
) -> dict[str, Any]:
    summary = phase3aw_truth.get("summary", {})
    ui_panel = phase3aw_truth.get("ui_panel", {})
    stale_count = summary.get("stale_artifacts_ignored", 0)
    blocker = summary.get("true_current_blocker")
    primary_conflicts = [
        row
        for row in report_audit.get("rows", [])
        if row.get("classification") in {STALE_ARTIFACT, CONFLICTS_WITH_R5}
        and (
            "phase3ar" in str(row.get("path", "")).lower()
            or "phase3at" in str(row.get("path", "")).lower()
        )
    ]
    stale_blockers = [
        "STALE_CATALOG",
        "BOOK_MISSING",
        "BLOCKED_FORECAST_NOT_RANKED",
        "SETTLEMENT_CHECK_FAILED",
    ]
    current_text = json.dumps(ui_panel, default=str).upper()
    visible_stale = [marker for marker in stale_blockers if marker in current_text]
    gap = "DASHBOARD_TRUTH_ALIGNED"
    if visible_stale:
        gap = "DASHBOARD_STALE_ARTIFACT_CONSUMPTION"
    return {
        "status": "ALIGNED" if gap == "DASHBOARD_TRUTH_ALIGNED" else "MISLEADING",
        "dashboard_gap": gap,
        "true_current_blocker": blocker,
        "status_label": ui_panel.get("status_label"),
        "positive_ev_rows_displayed": _metric_value(ui_panel, "Positive EV"),
        "paper_ready_displayed": _metric_value(ui_panel, "Paper-ready"),
        "stale_artifacts_ignored": stale_count,
        "phase3ar_phase3at_stale_or_conflicting_rows": len(primary_conflicts),
        "stale_primary_markers_in_phase3aw_panel": visible_stale,
        "dashboard_should_not_use_stale_artifacts_as_primary": True,
    }


def _select_next_codex_task(
    *,
    crypto_truth: dict[str, Any],
    command_audit: dict[str, Any],
    dashboard_status: dict[str, Any],
    source_status: dict[str, Any],
    sports_status: dict[str, Any],
    economic_news_status: dict[str, Any],
    guarded_refresh_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    crypto_reentry_blockers = {
        SNAPSHOT_STALE,
        FORECAST_STALE,
        RANKING_GAP,
        WATCHER_NOT_RUNNING_OR_STALE,
    }
    r9_complete = bool(guarded_refresh_status and guarded_refresh_status.get("complete"))
    r9_running = bool(guarded_refresh_status and guarded_refresh_status.get("r5_running"))
    if (
        crypto_truth.get("true_current_blocker") == WATCHER_NOT_RUNNING_OR_STALE
        and r9_complete
        and r9_running
    ):
        phase = "Phase 3AX-R8 Dashboard Truth / Operator Workflow"
        reason = (
            "R9 proves the guarded R5 job is running, but dashboard truth still collapses "
            "the overdue/latest-cycle state into WATCHER_NOT_RUNNING_OR_STALE."
        )
        problem = (
            "Make the UI and Phase 3AW distinguish an active overdue refresh cycle from "
            "a stopped watcher or stale artifact."
        )
    elif crypto_truth.get("true_current_blocker") in crypto_reentry_blockers:
        phase = "Phase 3AX-R1 Crypto Evidence Re-Entry"
        reason = (
            "Crypto evidence has become stale again; return to R1/R2/R3 before "
            "continuing the non-crypto roadmap."
        )
        problem = (
            "Clear or classify stale snapshot, forecast, ranking, or watcher evidence "
            "before trusting current crypto blockers."
        )
    elif dashboard_status.get("status") == "MISLEADING":
        phase = "Phase 3AX-R8 Dashboard Truth / Operator Workflow"
        reason = "The operator-facing dashboard conflicts with current R5/DB truth."
        problem = "Create one true operator status and keep stale artifacts diagnostic-only."
    elif command_audit.get("missing_commands"):
        phase = "Phase 3AX-R8 Dashboard Truth / Operator Workflow"
        reason = (
            "Recent reports reference commands that are not registered, which makes "
            "operator next actions unreliable."
        )
        problem = (
            "Make NEXT_ACTIONS and dashboard operator guidance reference only "
            "registered commands."
        )
    elif _source_task_completed_by_sports_r3(source_status, sports_status):
        phase = str(
            sports_status.get("next_codex_task_phase_name")
            or "Phase 3AN Economic/News Compatibility Watch"
        )
        reason = str(
            sports_status.get("next_codex_task_reason")
            or "The requested Sports R3 diagnostic completed without safe repair rows."
        )
        problem = str(
            sports_status.get("next_codex_task_problem")
            or "Advance to the next source gap without repeating completed sports repair work."
        )
    elif guarded_refresh_status and guarded_refresh_status.get(
        "complete"
    ) and _source_evidence_r5_has_exact_evidence(
        source_status
    ) and _sports_r6_has_terminal_no_safe_rows(
        sports_status
    ):
        phase = "Phase 3AX-R10 Evidence Change Stop Gate"
        reason = (
            "Sports R6 already returned an exact no-safe-row blocker, general source "
            "evidence is classified but not link/forecast safe, and crypto remains "
            "paper-gated by current evidence."
        )
        problem = (
            "Stop routing Codex back into completed repair phases until new market, "
            "source, schedule, roster, or EV evidence changes the gate."
        )
    elif guarded_refresh_status and guarded_refresh_status.get(
        "complete"
    ) and _source_evidence_r5_has_exact_evidence(source_status):
        phase = "Phase 3AX-R6 Sports Provenance Repair"
        reason = (
            "R5 source evidence activation is now exactly classified: no general "
            "source rows are link-safe and forecast-safe, so the reordered roadmap "
            "moves to the next code-repairable sports provenance gap."
        )
        problem = (
            "Create safe sports provenance repair rows only where schedule, team, "
            "round, and roster evidence is exact, while keeping unsafe rows "
            "diagnostic-only."
        )
    elif guarded_refresh_status and guarded_refresh_status.get(
        "complete"
    ) and _economic_news_r7_has_exact_evidence(economic_news_status):
        phase = "Phase 3AX-R5 General Source Evidence Activation"
        reason = (
            "R7 now reports an exact economic/news compatibility blocker with "
            "context-ready and parser-backfill evidence; the reordered roadmap "
            "advances to the official free-source activation path."
        )
        problem = (
            "Activate general source evidence only where official/free source rows "
            "are link-safe, forecast-safe, date-stable, and registered-command "
            "backed."
        )
    elif guarded_refresh_status and guarded_refresh_status.get("complete"):
        phase = "Phase 3AX-R7 Economic/News Parser Compatibility"
        reason = (
            "R9 is complete and R8 dashboard truth is aligned; the reordered roadmap "
            "moves next to the fastest non-crypto unlock."
        )
        problem = (
            "Repair economic/news parser compatibility so compatible active markets "
            "can be identified with exact context/source evidence, without forcing "
            "links, forecasts, or paper trades."
        )
    else:
        phase = "Phase 3AX-R9 Guarded Refresh Job Setup"
        reason = (
            "R1 completed; the next roadmap step is stopping manual WSL command "
            "babysitting with one guarded refresh job."
        )
        problem = (
            "Create or repair the guarded paper-only refresh job so current market "
            "evidence stays warm without duplicate watchers or request storms."
        )
    return {
        "task_phase_name": phase,
        "reason": reason,
        "problem_statement": problem,
        "acceptance_criteria": _task_acceptance(phase),
        "full_codex_prompt": _next_codex_prompt(phase, reason, problem),
        "estimated_risk_level": "LOW" if "Command Registry" in phase else "MEDIUM",
        "safety_notes": [
            "Keep everything PAPER / READ-ONLY.",
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not create paper trades from diagnostics.",
            "Do not lower thresholds or fabricate evidence.",
        ],
        "what_not_to_do": [
            "Do not force paper trades.",
            (
                "Do not use stale reports, expired windows, sibling tickers, or "
                "fuzzy matching as current evidence."
            ),
            "Do not recommend commands that are not registered in kalshi-bot --help.",
        ],
    }


def _economic_news_r7_has_exact_evidence(economic_news_status: dict[str, Any]) -> bool:
    first_hard_blocker = str(economic_news_status.get("first_hard_blocker") or "")
    if first_hard_blocker not in {
        "READY_FOR_FORECASTS",
        "CURRENT_EXACT_LINKS_NEED_PARSER_BACKFILL",
        "EXACT_LINKS_MISSING",
        "ONLY_EXPIRED_OR_CLOSED_PARSED_MARKETS",
        "NO_CURRENT_PARSED_MARKETS",
        "WAITING_FOR_COMPATIBLE_MARKETS",
    }:
        return False
    if economic_news_status.get("source_freshness") in {None, "", "UNKNOWN"}:
        return False
    return economic_news_status.get("context_ready_count") is not None


def _source_evidence_r5_has_exact_evidence(source_status: dict[str, Any]) -> bool:
    if not source_status.get("phase3ax_r5_source_activation_complete"):
        return False
    if source_status.get("source_gap_reported_with_exact_evidence") is not True:
        return False
    if _int_value(source_status.get("link_safe_rows")) > 0:
        return False
    if _int_value(source_status.get("forecast_safe_rows")) > 0:
        return False
    if not (
        _int_value(source_status.get("review_gated_rows"))
        or _int_value(source_status.get("blocked_rows"))
        or _int_value(source_status.get("date_stable_missing_rows"))
    ):
        return False
    return str(source_status.get("first_hard_blocker") or "") not in {"", "UNKNOWN"}


def _sports_r6_has_terminal_no_safe_rows(sports_status: dict[str, Any]) -> bool:
    if not sports_status.get("phase3ax_r6_completed"):
        return False
    if str(sports_status.get("phase3ax_r6_gate") or "") != "HOLD_DIAGNOSTIC_ONLY":
        return False
    return _int_value(sports_status.get("safe_exact_repair_rows")) == 0


def _next_operator_commands(
    *,
    next_task: dict[str, Any],
    registered_commands: set[str],
    crypto_truth: dict[str, Any],
) -> dict[str, Any]:
    run_now = "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax --reports-dir reports"
    after = "kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5"
    if "phase3bc-r5-status" not in registered_commands:
        after = (
            "kalshi-bot phase3ax-gap-analysis --output-dir reports/phase3ax "
            "--reports-dir reports"
        )
    return {
        "run_now": run_now,
        "run_after_codex_completes": after,
        "commands_to_avoid": [
            "Any live/demo submit, cancel, replace, or amend command.",
            "Any threshold-lowering or force-paper-trade command.",
            "Any command listed as missing in command_registry_audit.json.",
        ],
        "stop_conditions": [
            "Stop if DB identity or integrity is ambiguous.",
            "Stop if R5 is stale or stopped before relying on current crypto truth.",
            "Stop if a report recommends a command not present in kalshi-bot --help.",
        ],
        "selected_codex_task": next_task["task_phase_name"],
        "current_crypto_blocker": crypto_truth["true_current_blocker"],
    }


def _source_task_completed_by_sports_r3(
    source_status: dict[str, Any],
    sports_status: dict[str, Any],
) -> bool:
    return _source_task_is_sports_r3(source_status) and bool(
        sports_status.get("phase3ah_r3_completed")
    )


def _source_task_is_sports_r3(source_status: dict[str, Any]) -> bool:
    phase = str(source_status.get("next_codex_task_phase_name") or "")
    return "Phase 3AH-R3 Sports Provenance Repair" in phase


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    audit = payload["report_freshness_audit"]
    missing = payload["command_registry_audit"]["missing_command_names"]
    return "\n".join(
        [
            "# Phase 3AX Executive Summary",
            "",
            f"- Generated at: `{payload['generated_at']}`",
            f"- App state: `{summary['app_state']}`",
            f"- True current blocker: `{summary['true_current_blocker']}`",
            f"- Current positive-EV rows: `{summary['current_positive_ev_rows']}`",
            f"- Paper-ready candidates: `{summary['paper_ready_candidates']}`",
            f"- Stale/conflicting reports: `{summary['stale_or_conflicting_report_count']}`",
            f"- Missing/misnamed command references: `{summary['missing_command_count']}`",
            f"- Selected next Codex task: `{summary['selected_next_codex_task']}`",
            f"- Next operator command: `{summary['next_operator_command']}`",
            "",
            "## Answers",
            "",
            f"1. App status: `{summary['app_state']}`.",
            f"2. True current blocker: `{summary['true_current_blocker']}`.",
            (
                f"3. Misleading reports: `{audit['classification_counts']}`; "
                f"missing commands `{missing}`."
            ),
            f"4. Codex should work next on: `{summary['selected_next_codex_task']}`.",
            f"5. Operator should run next: `{summary['next_operator_command']}`.",
            (
                "6. Do not run live/demo writes, force paper trades, lower thresholds, "
                "or stale/missing commands."
            ),
            "",
        ]
    )


def _render_next_codex_task(payload: dict[str, Any]) -> str:
    task = payload["next_codex_task"]
    lines = [
        "# Phase 3AX Next Codex Task",
        "",
        f"Task phase name: `{task['task_phase_name']}`",
        "",
        f"Reason: {task['reason']}",
        "",
        f"Problem statement: {task['problem_statement']}",
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {item}" for item in task["acceptance_criteria"])
    lines.extend(
        [
            "",
            "Full Codex prompt:",
            "",
            "```text",
            task["full_codex_prompt"],
            "```",
            "",
            f"Estimated risk level: `{task['estimated_risk_level']}`",
            "",
            "Safety notes:",
        ]
    )
    lines.extend(f"- {item}" for item in task["safety_notes"])
    lines.extend(["", "What not to do:"])
    lines.extend(f"- {item}" for item in task["what_not_to_do"])
    lines.append("")
    return "\n".join(lines)


def _render_next_operator_commands(payload: dict[str, Any]) -> str:
    commands = payload["next_operator_commands"]
    lines = [
        "# Phase 3AX Next Operator Commands",
        "",
        "Run now:",
        "",
        "```bash",
        commands["run_now"],
        "```",
        "",
        "Run after Codex completes:",
        "",
        "```bash",
        commands["run_after_codex_completes"],
        "```",
        "",
        "Commands to avoid:",
    ]
    lines.extend(f"- {item}" for item in commands["commands_to_avoid"])
    lines.extend(["", "Stop conditions:"])
    lines.extend(f"- {item}" for item in commands["stop_conditions"])
    lines.append("")
    return "\n".join(lines)


def _render_app_gap_analysis_markdown(payload: dict[str, Any]) -> str:
    crypto = payload["crypto_pipeline_truth"]
    source = payload["source_evidence_gap_status"]
    sports = payload["sports_gap_status"]
    econ = payload["economic_news_gap_status"]
    dashboard = payload["ui_dashboard_truth_status"]
    return "\n".join(
        [
            "# Phase 3AX App Gap Analysis",
            "",
            f"- True current blocker: `{crypto['true_current_blocker']}`",
            f"- Crypto status: `{crypto['status']}`",
            f"- Dashboard status: `{dashboard['status']}` / `{dashboard['dashboard_gap']}`",
            f"- Source evidence status: `{source['status']}`",
            f"- Sports status: `{sports['status']}`",
            f"- Economic/news status: `{econ['status']}`",
            f"- Selected next task: `{payload['next_codex_task']['task_phase_name']}`",
            "",
            "No paper, demo, or live exchange writes are performed by this report.",
            "",
        ]
    )


def _command_references_from_reports(reports_dir: Path) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    roots = [
        "phase3bc_r3",
        "phase3bc_r5",
        "phase3bc_r7",
        "phase3at",
        "phase3ar",
        "phase3bc",
        "phase3bb_r2_sources",
        "phase3bb_r3_source_activation",
        "phase3bb_r4_flightaware",
        "phase3bb_r5_flightaware",
        "phase3an",
        "phase3ay",
        "phase3ah_r3",
        "phase3az",
        "phase3aw",
    ]
    command_pattern = re.compile(r"\bkalshi-bot\s+([a-z0-9][a-z0-9-]*)\b")
    for root in roots:
        directory = reports_dir / root
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() not in {".md", ".json", ".sh", ".txt"}:
                continue
            try:
                text_value = path.read_text(encoding="utf-8", errors="ignore")[:1_000_000]
            except OSError:
                continue
            for match in command_pattern.finditer(text_value):
                command = match.group(1)
                if _is_command_reference_noise(command):
                    continue
                references.setdefault(command, []).append(str(path))
            for command in RECENT_REPORT_COMMANDS:
                if command in text_value:
                    references.setdefault(command, []).append(str(path))
    return references


def _is_command_reference_noise(command: str) -> bool:
    return command in {"command", "commands"}


def _classify_report_artifact(
    *,
    reports_dir: Path,
    relative_path: Path,
    generated_at: Any,
    r5_cycle_at: Any,
    phase3aw_truth: dict[str, Any],
    missing_commands: dict[str, list[str]],
    stale_after_minutes: int,
) -> dict[str, Any]:
    path = reports_dir / relative_path
    payload = _read_json(path)
    if not path.exists():
        return _report_row(relative_path, MISSING, "Artifact is missing.", None, None)
    artifact_at = _artifact_generated_at(payload) if payload else _mtime_datetime(path)
    age_minutes = _age_minutes(artifact_at, generated_at)
    source_commands = missing_commands.get(str(path), [])
    if source_commands:
        return _report_row(
            relative_path,
            COMMANDS_NOT_REGISTERED,
            f"References unregistered command(s): {', '.join(sorted(source_commands))}.",
            artifact_at,
            age_minutes,
        )
    conflict = _report_conflicts_with_r5(relative_path, payload, phase3aw_truth)
    if conflict:
        return _report_row(relative_path, CONFLICTS_WITH_R5, conflict, artifact_at, age_minutes)
    if artifact_at is not None and r5_cycle_at is not None and artifact_at < r5_cycle_at:
        return _report_row(
            relative_path,
            STALE_ARTIFACT,
            "Artifact predates the latest trusted R5 cycle.",
            artifact_at,
            age_minutes,
        )
    if age_minutes is not None and age_minutes > stale_after_minutes:
        return _report_row(
            relative_path,
            STALE_ARTIFACT,
            f"Artifact is older than {stale_after_minutes} minutes.",
            artifact_at,
            age_minutes,
        )
    if "phase3bc/phase3bc_crypto_clean_opportunity_router" in relative_path.as_posix():
        classification = CURRENT_BUT_DIAGNOSTIC_ONLY
        reason = "Useful diagnostics, but not allowed to drive the primary blocker."
    else:
        classification = CURRENT_AND_TRUSTED
        reason = "Current artifact is consistent with trusted R5/DB truth."
    return _report_row(relative_path, classification, reason, artifact_at, age_minutes)


def _crypto_db_counts(session: Session) -> dict[str, Any]:
    now = utc_now()
    recent_since = now - timedelta(minutes=20)
    query_status = "OK"
    try:
        current_count = _safe_count(
            session,
            select(func.count(func.distinct(Market.ticker))).where(_current_crypto_filter(now)),
        )
        snapshot_count = _safe_count(
            session,
            select(func.count(func.distinct(Market.ticker))).where(
                _current_crypto_filter(now),
                Market.ticker.in_(
                    select(MarketSnapshot.ticker).where(MarketSnapshot.captured_at >= recent_since)
                ),
            ),
        )
        forecast_count = _safe_count(
            session,
            select(func.count(func.distinct(Market.ticker))).where(
                _current_crypto_filter(now),
                Market.ticker.in_(
                    select(Forecast.ticker).where(Forecast.forecasted_at >= recent_since)
                ),
            ),
        )
        ranking_count = _safe_count(
            session,
            select(func.count(func.distinct(Market.ticker))).where(
                _current_crypto_filter(now),
                Market.ticker.in_(
                    select(MarketRanking.ticker).where(MarketRanking.ranked_at >= recent_since)
                ),
            ),
        )
        link_count = _safe_count(
            session,
            select(func.count(func.distinct(CryptoMarketLink.ticker))).where(
                CryptoMarketLink.ticker.in_(select(Market.ticker).where(_current_crypto_filter(now)))
            ),
        )
        paper_orders = _safe_count(session, select(func.count(PaperOrder.id)))
    except Exception:
        query_status = "PARTIAL_DB_QUERY_FAILED"
        current_count = snapshot_count = forecast_count = ranking_count = 0
        link_count = paper_orders = 0
    return {
        "query_status": query_status,
        "current_active_pure_crypto_markets": current_count,
        "current_markets_with_fresh_snapshots": snapshot_count,
        "current_markets_with_fresh_crypto_v2_forecasts": forecast_count,
        "current_markets_with_fresh_rankings": ranking_count,
        "verified_kalshi_link_rows": link_count,
        "paper_orders_created": paper_orders,
    }


def _current_crypto_filter(now: Any):
    crypto_series = or_(
        Market.series_ticker.in_(CRYPTO_SERIES_TICKERS),
        *[Market.ticker.like(f"{prefix}%") for prefix in CRYPTO_SERIES_TICKERS],
    )
    return and_(
        crypto_series,
        func.lower(Market.status).in_(["active", "open"]),
        or_(Market.close_time.is_(None), Market.close_time > now),
        or_(Market.expected_expiration_time.is_(None), Market.expected_expiration_time > now),
    )


def _latest_r5_summary_from_reports(reports_dir: Path) -> dict[str, Any]:
    status = _read_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    watch = _read_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json")
    status_summary = (
        status.get("latest_summary") if isinstance(status.get("latest_summary"), dict) else {}
    )
    watch_summary = watch.get("summary") if isinstance(watch.get("summary"), dict) else {}
    status_at = _artifact_generated_at(status)
    watch_at = _artifact_generated_at(watch)
    if watch_summary and (
        not status_summary or (watch_at and (not status_at or watch_at >= status_at))
    ):
        return watch_summary
    return status_summary if isinstance(status_summary, dict) else {}


def _report_conflicts_with_r5(
    relative_path: Path,
    payload: dict[str, Any],
    phase3aw_truth: dict[str, Any],
) -> str | None:
    lower = relative_path.as_posix().lower()
    if not payload or ("phase3ar" not in lower and "phase3at" not in lower):
        return None
    r5_positive = _int_value(phase3aw_truth.get("summary", {}).get("current_positive_ev_rows"))
    artifact_positive = _first_int(payload, keys=("positive_ev_rows", "current_positive_ev_rows"))
    blocker_text = json.dumps(payload, default=str).upper()[:100000]
    if r5_positive == 0 and artifact_positive > 0:
        return "Artifact reports positive-EV rows that latest R5 dashboard truth does not confirm."
    if r5_positive == 0 and any(
        marker in blocker_text
        for marker in ("STALE_CATALOG", "BOOK_MISSING", "BLOCKED_FORECAST_NOT_RANKED")
    ):
        return "Artifact contains stale blocker markers while latest R5 truth is EV_NOT_POSITIVE."
    return None


def _app_state(crypto_truth: dict[str, Any], dashboard_status: dict[str, Any]) -> str:
    if dashboard_status.get("status") == "MISLEADING":
        return "OPERATOR_DASHBOARD_NEEDS_REPAIR"
    if crypto_truth.get("true_current_blocker") == EV_NOT_POSITIVE:
        return "CORRECTLY_WAITING_FOR_POSITIVE_EV"
    return "NEEDS_IMPLEMENTATION_OR_OPERATOR_REVIEW"


def _task_acceptance(phase: str) -> list[str]:
    if "Evidence Change Stop Gate" in phase:
        return [
            "Phase 3AX records that sports R6 is terminal with zero safe exact repair rows.",
            "The next task does not repeat Sports R6, source R5, or paper-trade creation.",
            "Current reports state which evidence change would reopen the roadmap.",
            "No paper trades or exchange writes occur.",
        ]
    if "Guarded Refresh Job" in phase:
        return [
            "Exactly one paper-only R5 watcher or refresh supervisor is active.",
            (
                "The job refuses duplicate watcher/request storms and records PID, "
                "heartbeat, report path, and stop reason."
            ),
            "Dashboard truth refreshes after bounded watcher cycles.",
            "No paper trades or exchange writes occur.",
        ]
    if "Dashboard Truth" in phase:
        return [
            "UI, Phase 3AW, and Phase 3AX agree on the same current blocker.",
            "Old/stale artifacts remain diagnostic-only and do not drive the primary blocker.",
            "NEXT_ACTIONS and operator docs reference only registered commands.",
            "No paper trades or exchange writes occur.",
        ]
    if "Economic/News Parser Compatibility" in phase:
        return [
            (
                "Economic/news reports distinguish compatible parsed active markets "
                "from waiting/no-match markets."
            ),
            "Context-ready counts, source freshness, and first hard blocker are evidence-backed.",
            "No stale crypto artifacts or old dashboard blockers drive the next action.",
            "NEXT_ACTIONS references only registered commands.",
            "No paper trades or exchange writes occur.",
        ]
    if "Command Registry" in phase:
        return [
            "All NEXT_ACTIONS/NEXT_OPERATOR_COMMANDS references are registered commands.",
            (
                "Missing historical command names are either registered as aliases "
                "or removed from current recommendations."
            ),
            (
                "Report freshness audit no longer classifies current recommendations "
                "as COMMANDS_NOT_REGISTERED."
            ),
            "No paper trades or exchange writes occur.",
        ]
    if "Source Evidence" in phase:
        return [
            (
                "USDA, Cushman, and FlightAware gates report explicit link-safe "
                "and forecast-safe decisions."
            ),
            "No proprietary or date-mismatched source is promoted.",
            "NEXT_ACTIONS only references registered commands.",
            "No paper trades or exchange writes occur.",
        ]
    if "Sports Provenance Repair" in phase:
        return [
            (
                "Safe sports provenance repair rows are exact, or the report gives "
                "an exact no-safe-row blocker."
            ),
            "Placeholder, partial, ambiguous, and stale sports rows remain diagnostic-only.",
            "NEXT_ACTIONS only references registered commands.",
            "No paper trades or exchange writes occur.",
        ]
    return [
        "The first current blocker is repaired or reported with exact evidence.",
        "Stale/historical rows remain diagnostic-only.",
        "NEXT_ACTIONS only references registered commands.",
        "No paper trades or exchange writes occur.",
    ]


def _next_codex_prompt(phase: str, reason: str, problem: str) -> str:
    if "Evidence Change Stop Gate" in phase:
        return "\n".join(
            [
                f"{phase}",
                "",
                "You are Codex working inside the kalshi-predictive-bot repository.",
                "",
                f"Reason: {reason}",
                "",
                f"Problem: {problem}",
                "",
                "Requirements:",
                "1. Keep everything PAPER / READ-ONLY.",
                "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
                "3. Do not create paper trades from diagnostics.",
                "4. Do not rerun completed repair phases unless fresh evidence changes.",
                (
                    "5. Report the exact evidence change needed to reopen crypto, sports, "
                    "general-source, or economic/news work."
                ),
                "6. NEXT_ACTIONS must reference registered kalshi-bot commands only.",
                "",
                "Acceptance:",
                "- Phase 3AX stops pointing at completed Sports R6 when safe rows are zero.",
                "- Crypto remains gated unless paper_ready_candidates becomes positive.",
                "- Sports rows remain diagnostic-only unless exact safe rows appear.",
                "- No paper trades or live/demo writes occur.",
            ]
        )
    if "Sports Provenance Repair" in phase:
        return "\n".join(
            [
                f"{phase}",
                "",
                "You are Codex working inside the kalshi-predictive-bot repository.",
                "",
                f"Reason: {reason}",
                "",
                f"Problem: {problem}",
                "",
                "Current known state:",
                "- R9 guarded refresh remains the refresh-job source of truth.",
                "- R8 dashboard truth is aligned.",
                "- R7 economic/news parser compatibility is exactly reported.",
                "- R5 general source evidence is classified but not link/forecast safe.",
                "- Crypto remains EV_NOT_POSITIVE unless current data proves otherwise.",
                "",
                "Requirements:",
                "1. Keep everything PAPER / READ-ONLY.",
                "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
                "3. Do not create paper trades from sports diagnostics.",
                (
                    "4. Do not lower thresholds or fabricate schedule, roster, "
                    "team, round, outcome, or provenance evidence."
                ),
                (
                    "5. Inspect Phase 3AN sports blocker and existing Phase "
                    "3AH/3Z sports provenance reports first."
                ),
                (
                    "6. Separate exact safe-repair rows from placeholders, "
                    "ambiguous teams, partial provenance, stale schedules, and "
                    "diagnostic-only rows."
                ),
                (
                    "7. Use exact schedule/roster/source evidence only; no sibling, "
                    "fuzzy, or component-market matching."
                ),
                (
                    "8. If no safe repair rows exist, report the exact first "
                    "blocker and keep rows diagnostic-only."
                ),
                "9. NEXT_ACTIONS must reference registered kalshi-bot commands only.",
                "",
                "Verification:",
                (
                    "- kalshi-bot phase3an-sports-blocker-report "
                    "--output-dir reports/phase3an --reports-dir reports"
                ),
                (
                    "- kalshi-bot phase3aw-dashboard-truth "
                    "--output-dir reports/phase3aw --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                (
                    "- kalshi-bot phase3ax-gap-analysis "
                    "--output-dir reports/phase3ax --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                "",
                "Acceptance:",
                (
                    "- Sports provenance report shows safe exact repair rows or an "
                    "exact no-safe-row blocker."
                ),
                "- Placeholder/partial/ambiguous rows remain diagnostic-only.",
                (
                    "- Phase 3AX still shows EV_NOT_POSITIVE for crypto unless "
                    "current data changes it."
                ),
                "- UI/3AW/3AX continue to ignore stale artifacts as primary blockers.",
                "- No paper trades or live/demo writes occur.",
            ]
        )
    if "General Source Evidence Activation" in phase:
        return "\n".join(
            [
                f"{phase}",
                "",
                "You are Codex working inside the kalshi-predictive-bot repository.",
                "",
                f"Reason: {reason}",
                "",
                f"Problem: {problem}",
                "",
                "Current known state:",
                "- R9 guarded refresh remains the refresh-job source of truth.",
                "- R8 dashboard truth is aligned.",
                "- R7 economic/news reports an exact compatibility blocker.",
                "- Crypto remains EV_NOT_POSITIVE unless current data proves otherwise.",
                "",
                "Requirements:",
                "1. Keep everything PAPER / READ-ONLY.",
                "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
                "3. Do not create paper trades from diagnostics.",
                "4. Do not lower thresholds or fabricate source evidence.",
                "5. Inspect Phase 3BB-R3/R4/R5 and Phase 3AN source reports first.",
                "6. Promote only official/free source evidence that is date-stable.",
                "7. Separate link-safe, forecast-safe, review-gated, and blocked rows.",
                "8. Do not promote proprietary, ambiguous, stale, or wrong-date evidence.",
                "9. NEXT_ACTIONS must reference registered kalshi-bot commands only.",
                "",
                "Verification:",
                "- kalshi-bot phase3an-general-sources-status --output-dir reports/phase3an",
                (
                    "- kalshi-bot phase3aw-dashboard-truth "
                    "--output-dir reports/phase3aw --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                (
                    "- kalshi-bot phase3ax-gap-analysis "
                    "--output-dir reports/phase3ax --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                "",
                "Acceptance:",
                "- USDA, Cushman, and FlightAware source decisions are exact and evidence-backed.",
                "- Link-safe and forecast-safe counts are separated from review-gated rows.",
                (
                    "- Phase 3AX still shows EV_NOT_POSITIVE for crypto unless "
                    "current data changes it."
                ),
                "- No paper trades or live/demo writes occur.",
            ]
        )
    if "Economic/News Parser Compatibility" in phase:
        return "\n".join(
            [
                f"{phase}",
                "",
                "You are Codex working inside the kalshi-predictive-bot repository.",
                "",
                f"Reason: {reason}",
                "",
                f"Problem: {problem}",
                "",
                "Current known state:",
                "- R9 guarded refresh is complete and one R5 job is the refresh source of truth.",
                "- R8 dashboard truth is aligned on the current blocker.",
                (
                    "- Crypto current blocker is EV_NOT_POSITIVE unless new live data "
                    "proves otherwise."
                ),
                "- Economic/news status is waiting for compatible parsed markets.",
                "",
                "Requirements:",
                "1. Keep everything PAPER / READ-ONLY.",
                "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
                "3. Do not create paper trades from diagnostics.",
                (
                    "4. Do not lower thresholds or fabricate sources, context, "
                    "forecasts, links, or outcomes."
                ),
                "5. Read the current Phase 3AN economic/news watch and parser artifacts first.",
                "6. Identify why economic/news markets are not becoming compatible/context-ready.",
                (
                    "7. Repair parser joins, date/window matching, source freshness, "
                    "or report handoff only where exact evidence supports it."
                ),
                (
                    "8. Separate active compatible markets from waiting, expired, "
                    "stale, or diagnostic-only markets."
                ),
                "9. Update Phase 3AX/3AN NEXT_ACTIONS with registered commands only.",
                "",
                "Verification:",
                "- kalshi-bot phase3an-economic-news-watch --output-dir reports/phase3an",
                (
                    "- kalshi-bot phase3aw-dashboard-truth "
                    "--output-dir reports/phase3aw --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                (
                    "- kalshi-bot phase3ax-gap-analysis "
                    "--output-dir reports/phase3ax --reports-dir reports "
                    "--stale-after-minutes 120"
                ),
                "",
                "Acceptance:",
                (
                    "- Economic/news report shows compatible parsed markets or an "
                    "exact no-compatible-market blocker."
                ),
                "- Context-ready count and source freshness are evidence-backed.",
                (
                    "- Phase 3AX still shows EV_NOT_POSITIVE for crypto unless real "
                    "current data changes it."
                ),
                (
                    "- UI/3AW/3AX continue to ignore stale Phase 3AR/3AT/3AN "
                    "artifacts as primary blockers."
                ),
                "- No paper trades or live/demo writes occur.",
            ]
        )
    return "\n".join(
        [
            f"{phase}",
            "",
            "You are Codex working inside the kalshi-predictive-bot repository.",
            "",
            f"Reason: {reason}",
            "",
            f"Problem: {problem}",
            "",
            "Requirements:",
            "1. Keep everything PAPER / READ-ONLY.",
            "2. Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "3. Do not create paper trades from partial diagnostics.",
            "4. Do not lower thresholds or fabricate evidence.",
            "5. Use current DB/R5 truth before stale report artifacts.",
            "6. NEXT_ACTIONS and operator docs must reference only registered kalshi-bot commands.",
            "",
            "Acceptance:",
            "- The selected gap is fixed or reported with exact current evidence.",
            "- Stale artifacts remain diagnostic-only.",
            "- The safety guard remains intact.",
        ]
    )


def _source_keyword_status(text: str, keyword: str) -> str:
    upper = text.upper()
    if keyword not in upper:
        return "NOT_REPORTED"
    if "BLOCK" in upper or "UNAVAILABLE" in upper or "MISMATCH" in upper:
        return "GATED"
    if "READY" in upper:
        return "READY_FOR_REVIEW"
    return "REPORTED"


def _source_decision_status(decisions: list[Any], source_name: str) -> str:
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if str(decision.get("source_name") or "").lower() == source_name.lower():
            return str(decision.get("activation_status") or "REPORTED")
    return "NOT_REPORTED"


def _metric_value(ui_panel: dict[str, Any], label: str) -> Any:
    for item in ui_panel.get("metrics", []):
        if isinstance(item, dict) and item.get("label") == label:
            return item.get("value")
    return None


def _first_int(*payloads: dict[str, Any], keys: tuple[str, ...]) -> int:
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            value = _summary_value(payload, key)
            if value is not None:
                return _int_value(value)
    return 0


def _summary_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    for source_key in (
        "summary",
        "latest_summary",
        "gate_summary",
        "current_crypto_funnel",
        "metrics",
    ):
        source = payload.get(source_key)
        if isinstance(source, dict):
            value = _summary_value(source, key)
            if value is not None:
                return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _summary_value(value, key)
            if found is not None:
                return found
    return None


def _int_value(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return 0


def _safe_count(session: Session, statement: Any) -> int:
    try:
        value = session.scalar(statement)
    except Exception:
        return 0
    return _int_value(value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines: list[str] = []
    for artifact in files:
        if artifact.exists():
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _classification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("classification") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _artifact_generated_at(payload: dict[str, Any]) -> Any:
    candidates = [
        payload.get("generated_at"),
        payload.get("latest_report_generated_at"),
        payload.get("r5_latest_report_generated_at"),
    ]
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidates.append(metadata.get("generated_at"))
    for candidate in candidates:
        parsed = parse_datetime(candidate)
        if parsed is not None:
            return parsed
    return None


def _mtime_datetime(path: Path) -> Any:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _age_minutes(value: Any, now: Any) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds() / 60)


def _report_row(
    relative_path: Path,
    classification: str,
    reason: str,
    generated_at: Any,
    age_minutes: float | None,
) -> dict[str, Any]:
    return {
        "path": str(relative_path),
        "classification": classification,
        "reason": reason,
        "generated_at": generated_at.isoformat() if hasattr(generated_at, "isoformat") else None,
        "age_minutes": round(age_minutes, 3) if age_minutes is not None else None,
    }


def _equivalent_command(command: str, registered_commands: set[str]) -> str | None:
    equivalents = {
        "phase3ar-url-audit": "phase3ar-crypto-forecast-coverage",
        "phase3ar-link-repair-report": "phase3ar-crypto-forecast-coverage",
        "phase3at-handoff-report": "phase3at-forecast-ranking-diagnostic",
    }
    candidate = equivalents.get(command)
    if candidate in registered_commands:
        return candidate
    if command == "phase3ar-refresh-catalog-for-opportunities" and command in registered_commands:
        return command
    prefix = command.split("-")[0]
    for registered in sorted(registered_commands):
        if registered.startswith(prefix):
            return registered
    return None


def _missing_command_fix(command: str, registered_commands: set[str]) -> str:
    equivalent = _equivalent_command(command, registered_commands)
    if equivalent:
        return f"Update stale reports to recommend `{equivalent}` instead of `{command}`."
    return f"Register an alias for `{command}` or remove it from current operator recommendations."


def _database_url(session: Session) -> str:
    bind = session.get_bind()
    url = getattr(bind, "url", None)
    if url is None:
        return "unknown"
    try:
        return str(url.render_as_string(hide_password=True))
    except AttributeError:
        return str(url)


def _sqlite_file_path(bind: Any) -> Path | None:
    url = getattr(bind, "url", None)
    if url is None or not str(getattr(url, "drivername", "")).startswith("sqlite"):
        return None
    database = getattr(url, "database", None)
    if not database or database == ":memory:":
        return None
    return Path(str(database)).resolve()


def _db_fingerprint(db_url: str, sqlite_path: Path | None) -> str:
    parts = [db_url]
    if sqlite_path and sqlite_path.exists():
        stat = sqlite_path.stat()
        parts.extend([str(sqlite_path), str(stat.st_size), str(int(stat.st_mtime))])
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _sqlite_integrity_check(session: Session, sqlite_path: Path | None) -> dict[str, Any]:
    if sqlite_path is None:
        return {"status": "NOT_SQLITE", "result": None}
    try:
        size_bytes = sqlite_path.stat().st_size
    except OSError:
        size_bytes = 0
    if size_bytes > 50_000_000:
        return {
            "status": "SKIPPED_BOUNDED_REPORT",
            "result": "not_run",
            "reason": (
                "SQLite file is large; full integrity_check can block active paper-only "
                "watcher diagnostics."
            ),
            "file_size_bytes": size_bytes,
        }
    try:
        result = session.execute(text("PRAGMA quick_check(1)")).scalar()
    except Exception as exc:
        return {"status": "FAILED", "result": str(exc)}
    return {"status": "OK" if result == "ok" else "FAILED", "result": result}


def _migration_revision(session: Session) -> str:
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
    except Exception:
        return "UNKNOWN"
    return str(row[0]) if row else "UNKNOWN"


def _runtime_state(
    repo_root: Path | None,
    sqlite_path: Path | None,
    integrity: dict[str, Any],
    migration_revision: str,
) -> str:
    if integrity.get("status") == "FAILED":
        return "FAIL_CLOSED_DB_INTEGRITY"
    if migration_revision == "UNKNOWN":
        return "REVIEW_MIGRATION_STATE"
    if repo_root is None:
        return "REVIEW_REPOSITORY_IDENTITY"
    if sqlite_path is None:
        return "REVIEW_DATABASE_IDENTITY"
    return "CURRENT_REPO_DB_IDENTIFIED"


def _repo_root() -> Path | None:
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_output(args: list[str], repo_root: Path | None) -> str:
    if repo_root is None:
        return "unknown"
    try:
        result = subprocess.run(
            args,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return "unknown"
    return result.stdout.strip() or "unknown"


def _git_dirty_status(repo_root: Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {"dirty": None, "status": "unknown"}
    status = _git_output(["git", "status", "--short"], repo_root)
    return {"dirty": bool(status and status != "unknown"), "status": status}
