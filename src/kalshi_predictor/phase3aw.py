from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.phase3au import (
    DEFAULT_EVENTS_FILE,
    DEFAULT_HEARTBEAT_DIR,
    format_elapsed,
    load_latest_long_job_status,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

DEFAULT_OUTPUT_DIR = Path("reports/phase3aw")
DEFAULT_MARKDOWN = "phase3aw_crash_recovery.md"
DEFAULT_JSON = "phase3aw_crash_recovery.json"
PHASE3AW_DASHBOARD_VERSION = "phase3aw_dashboard_truth_v1"

DASHBOARD_TRUTH_JSON = "dashboard_truth.json"
STALE_ARTIFACT_AUDIT_JSON = "stale_artifact_audit.json"
CURRENT_CRYPTO_FUNNEL_JSON = "current_crypto_funnel.json"
CURRENT_CRYPTO_FUNNEL_MD = "current_crypto_funnel.md"
EXECUTIVE_SUMMARY_MD = "EXECUTIVE_SUMMARY.md"
NEXT_ACTIONS_MD = "NEXT_ACTIONS.md"
OPERATOR_NEXT_COMMAND_SH = "operator_next_command.sh"
MANIFEST_SHA256 = "MANIFEST.sha256"

CURRENT_AND_TRUSTED = "CURRENT_AND_TRUSTED"
CURRENT_BUT_DIAGNOSTIC_ONLY = "CURRENT_BUT_DIAGNOSTIC_ONLY"
STALE_ARTIFACT = "STALE_ARTIFACT"
CONFLICTS_WITH_DB = "CONFLICTS_WITH_DB"
CONFLICTS_WITH_R5 = "CONFLICTS_WITH_R5"
MISSING = "MISSING"
COMMANDS_NOT_REGISTERED = "COMMANDS_NOT_REGISTERED"

EV_NOT_POSITIVE = "EV_NOT_POSITIVE"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
FORECAST_STALE = "FORECAST_STALE"
RANKING_GAP = "RANKING_GAP"
WATCHER_NOT_RUNNING_OR_STALE = "WATCHER_NOT_RUNNING_OR_STALE"
RUNNING_CYCLE_OVERDUE = "RUNNING_CYCLE_OVERDUE"
NO_CURRENT_ACTIVE_CRYPTO_MARKETS = "NO_CURRENT_ACTIVE_CRYPTO_MARKETS"
EXECUTABLE_EV_NOT_POSITIVE = "EXECUTABLE_EV_NOT_POSITIVE"
LIQUIDITY_OR_SPREAD_BLOCK = "LIQUIDITY_OR_SPREAD_BLOCK"
LOW_EDGE_OR_SCORE_BLOCK = "LOW_EDGE_OR_SCORE_BLOCK"
URL_OR_CATALOG_BLOCK = "URL_OR_CATALOG_BLOCK"
RISK_OR_SIZE_BLOCK = "RISK_OR_SIZE_BLOCK"
PAPER_READY_CANDIDATE_AVAILABLE = "PAPER_READY_CANDIDATE_AVAILABLE"
PAPER_ORDER_CREATION_BLOCKED = "PAPER_ORDER_CREATION_BLOCKED"

DASHBOARD_ARTIFACTS = (
    ("Phase 3BC-R5 status", Path("phase3bc_r5/phase3bc_r5_status.json"), True),
    (
        "Phase 3BC-R5 watch report",
        Path("phase3bc_r5/phase3bc_r5_crypto_freshness_watch.json"),
        True,
    ),
    (
        "Phase 3BC-R3 active crypto refresh",
        Path("phase3bc_r3/phase3bc_r3_active_crypto_refresh.json"),
        True,
    ),
    (
        "Phase 3BC-R7 ranking coverage",
        Path("phase3bc_r7/phase3bc_r7_crypto_ranking_coverage_repair.json"),
        True,
    ),
    ("Phase 3AT active router", Path("phase3at/phase3at_active_router.json"), False),
    (
        "Phase 3AT forecast/ranking diagnostic",
        Path("phase3at/forecast_ranking_diagnostic.json"),
        False,
    ),
    ("Phase 3AT opportunity funnel", Path("phase3at/opportunity_funnel.json"), False),
    (
        "Phase 3BC clean opportunity router",
        Path("phase3bc/phase3bc_crypto_clean_opportunity_router.json"),
        False,
    ),
    (
        "Phase 3AR crypto forecast coverage",
        Path("phase3ar/phase3ar_crypto_forecast_coverage.json"),
        False,
    ),
    (
        "Phase 3AR paper-ready gate",
        Path("phase3ar/paper_ready_gate_after_url_repair.json"),
        False,
    ),
    ("Phase 3AR URL audit", Path("phase3ar/url_audit.json"), False),
    ("Phase 3AR catalog refresh", Path("phase3ar/catalog_refresh_plan.json"), False),
)

RUNNING = "RUNNING"
COMPLETED_CLEANLY = "COMPLETED_CLEANLY"
STOPPED_EARLY = "STOPPED_EARLY"
CRASHED_OR_INTERRUPTED = "CRASHED_OR_INTERRUPTED"
HEARTBEAT_STALLED = "HEARTBEAT_STALLED"
LEGACY_WRITER_ACTIVE = "LEGACY_WRITER_ACTIVE"
NO_HEARTBEAT = "NO_HEARTBEAT"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Phase3AWDashboardTruthArtifactSet:
    output_dir: Path
    dashboard_truth_path: Path
    stale_artifact_audit_path: Path
    current_crypto_funnel_path: Path
    current_crypto_funnel_markdown_path: Path
    executive_summary_path: Path
    next_actions_path: Path
    operator_next_command_path: Path
    manifest_path: Path


def build_phase3aw_dashboard_truth(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    stale_after_minutes: int = 120,
) -> dict[str, Any]:
    """Resolve the current paper-trade blocker from trusted crypto watch truth."""

    resolved = settings or get_settings()
    generated_at = utc_now()
    r5_status = _read_json(reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json")
    r5_watch = _read_json(
        reports_dir / "phase3bc_r5" / "phase3bc_r5_crypto_freshness_watch.json"
    )
    r5_summary = _latest_r5_summary(r5_status, r5_watch)
    r5_guard = r5_status.get("guard") if isinstance(r5_status.get("guard"), dict) else {}
    r5_cycle_at = (
        parse_datetime(r5_status.get("latest_report_generated_at"))
        or parse_datetime(r5_watch.get("generated_at"))
        or parse_datetime(r5_status.get("generated_at"))
    )
    phase3at_diagnostic = _read_json(
        reports_dir / "phase3at" / "forecast_ranking_diagnostic.json"
    )
    current_scope_summary = _current_scope_summary_from_artifacts(
        r5_summary=r5_summary,
        phase3at_diagnostic=phase3at_diagnostic,
    )
    metadata = _dashboard_metadata(
        session,
        settings=resolved,
        command_args=command_args or [],
        generated_at=generated_at,
        reports_dir=reports_dir,
        r5_status=r5_status,
        r5_watch=r5_watch,
        phase3at_diagnostic=phase3at_diagnostic,
    )
    artifact_audit = [
        _artifact_audit_row(
            reports_dir=reports_dir,
            name=name,
            relative_path=relative_path,
            trusted_source=trusted_source,
            r5_summary=r5_summary,
            r5_cycle_at=r5_cycle_at,
            generated_at=generated_at,
            stale_after_minutes=stale_after_minutes,
        )
        for name, relative_path, trusted_source in DASHBOARD_ARTIFACTS
    ]
    true_blocker = _true_dashboard_blocker(
        r5_summary=r5_summary,
        r5_guard=r5_guard,
        current_scope_summary=current_scope_summary,
    )
    stale_artifacts_ignored = sum(
        1
        for row in artifact_audit
        if row["classification"]
        in {STALE_ARTIFACT, CONFLICTS_WITH_DB, CONFLICTS_WITH_R5, COMMANDS_NOT_REGISTERED}
    )
    current_funnel = _current_crypto_funnel(
        r5_summary=r5_summary,
        r5_guard=r5_guard,
        current_scope_summary=current_scope_summary,
        true_blocker=true_blocker,
        stale_artifacts_ignored=stale_artifacts_ignored,
    )
    next_command = _operator_next_command(true_blocker)
    ui_panel = _ui_panel(
        current_funnel=current_funnel,
        true_blocker=true_blocker,
        stale_artifacts_ignored=stale_artifacts_ignored,
        next_command=next_command,
        r5_status_path=reports_dir / "phase3bc_r5" / "phase3bc_r5_status.json",
    )
    return {
        "generated_at": generated_at.isoformat(),
        "phase": "3AW",
        "phase_version": PHASE3AW_DASHBOARD_VERSION,
        "mode": "PAPER_ONLY_DASHBOARD_TRUTH_RECONCILIATION",
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "metadata": metadata,
        "summary": {
            "bot_state": _bot_state(true_blocker),
            "true_current_blocker": true_blocker,
            "true_current_blocker_label": _display_blocker_label(
                true_blocker,
                current_funnel,
            ),
            "r5_running": bool(r5_guard.get("running")),
            "r5_guard_status": r5_guard.get("status") or "UNKNOWN",
            "r5_runner_state": current_funnel["r5_runner_state"],
            "r5_stale_report": current_funnel["r5_stale_report"],
            "r5_latest_report_generated_at": _iso_or_none(r5_cycle_at),
            "current_active_crypto_markets": current_funnel[
                "current_active_crypto_markets"
            ],
            "snapshots_fresh": current_funnel["snapshots_fresh"],
            "forecasts_fresh": current_funnel["forecasts_fresh"],
            "rankings_fresh": current_funnel["rankings_fresh"],
            "current_positive_ev_rows": current_funnel["current_positive_ev_rows"],
            "clean_execution_rows": current_funnel["clean_execution_rows"],
            "paper_ready_candidates": current_funnel["paper_ready_candidates"],
            "best_current_expected_value_cents": current_funnel[
                "best_current_expected_value_cents"
            ],
            "best_ev_gap_to_positive_cents": current_funnel[
                "best_ev_gap_to_positive_cents"
            ],
            "best_ev_candidate_ticker": current_funnel["best_ev_candidate_ticker"],
            "stale_artifacts_ignored": stale_artifacts_ignored,
            "operator_next_command": next_command,
        },
        "current_crypto_funnel": current_funnel,
        "stale_artifact_audit": {
            "generated_at": generated_at.isoformat(),
            "r5_latest_report_generated_at": _iso_or_none(r5_cycle_at),
            "stale_after_minutes": stale_after_minutes,
            "rows": artifact_audit,
            "classification_counts": _classification_counts(artifact_audit),
        },
        "ui_panel": ui_panel,
        "operator_next_command": next_command,
        "operator_do_not_run": [
            "Do not force paper trades.",
            (
                "Do not lower EV, score, confidence, liquidity, spread, settlement, "
                "or risk thresholds."
            ),
            "Do not run live/demo submit, cancel, replace, or amend commands.",
            "Do not treat old Phase 3AR URL audit rows as current paper-ready blockers.",
        ],
    }


def write_phase3aw_dashboard_truth_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = Path("reports"),
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    stale_after_minutes: int = 120,
) -> Phase3AWDashboardTruthArtifactSet:
    payload = build_phase3aw_dashboard_truth(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        stale_after_minutes=stale_after_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard_truth_path = output_dir / DASHBOARD_TRUTH_JSON
    stale_artifact_audit_path = output_dir / STALE_ARTIFACT_AUDIT_JSON
    current_crypto_funnel_path = output_dir / CURRENT_CRYPTO_FUNNEL_JSON
    current_crypto_funnel_markdown_path = output_dir / CURRENT_CRYPTO_FUNNEL_MD
    executive_summary_path = output_dir / EXECUTIVE_SUMMARY_MD
    next_actions_path = output_dir / NEXT_ACTIONS_MD
    operator_next_command_path = output_dir / OPERATOR_NEXT_COMMAND_SH
    manifest_path = output_dir / MANIFEST_SHA256

    _write_json(dashboard_truth_path, payload)
    _write_json(stale_artifact_audit_path, payload["stale_artifact_audit"])
    _write_json(current_crypto_funnel_path, payload["current_crypto_funnel"])
    current_crypto_funnel_markdown_path.write_text(
        _render_current_crypto_funnel(payload),
        encoding="utf-8",
    )
    executive_summary_path.write_text(
        _render_dashboard_truth_summary(payload),
        encoding="utf-8",
    )
    next_actions_path.write_text(_render_dashboard_next_actions(payload), encoding="utf-8")
    operator_next_command_path.write_text(
        _render_operator_next_command(payload),
        encoding="utf-8",
    )
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            next_actions_path,
            dashboard_truth_path,
            stale_artifact_audit_path,
            current_crypto_funnel_path,
            current_crypto_funnel_markdown_path,
            operator_next_command_path,
        ],
    )
    return Phase3AWDashboardTruthArtifactSet(
        output_dir=output_dir,
        dashboard_truth_path=dashboard_truth_path,
        stale_artifact_audit_path=stale_artifact_audit_path,
        current_crypto_funnel_path=current_crypto_funnel_path,
        current_crypto_funnel_markdown_path=current_crypto_funnel_markdown_path,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        operator_next_command_path=operator_next_command_path,
        manifest_path=manifest_path,
    )


def _latest_r5_summary(
    r5_status: dict[str, Any],
    r5_watch: dict[str, Any],
) -> dict[str, Any]:
    status_summary = r5_status.get("latest_summary")
    if not isinstance(status_summary, dict):
        status_summary = {}
    watch_summary = r5_watch.get("summary")
    if not isinstance(watch_summary, dict):
        watch_summary = {}
    if not status_summary:
        return watch_summary
    if not watch_summary:
        return status_summary
    status_generated_at = _artifact_generated_at(r5_status)
    watch_generated_at = _artifact_generated_at(r5_watch)
    if (
        watch_generated_at is not None
        and (status_generated_at is None or watch_generated_at >= status_generated_at)
    ):
        return watch_summary
    return status_summary


def _current_scope_summary_from_artifacts(
    *,
    r5_summary: dict[str, Any],
    phase3at_diagnostic: dict[str, Any],
) -> dict[str, Any]:
    phase3at_summary = (
        phase3at_diagnostic.get("summary")
        if isinstance(phase3at_diagnostic.get("summary"), dict)
        else {}
    )
    active = max(
        _int_value(r5_summary.get("current_active_window_rows")),
        _int_value(r5_summary.get("active_pure_crypto_rows")),
        _int_value(phase3at_summary.get("current_active_crypto_markets")),
    )
    current_snapshots = max(
        _int_value(phase3at_summary.get("current_snapshots")),
        active
        - _int_value(r5_summary.get("snapshot_stale_rows"))
        - _int_value(r5_summary.get("snapshot_missing_rows")),
    )
    return {
        "current_active_crypto_markets": active,
        "current_active_crypto_markets_total": active,
        "current_snapshots": max(0, current_snapshots),
    }


def _true_dashboard_blocker(
    *,
    r5_summary: dict[str, Any],
    r5_guard: dict[str, Any],
    current_scope_summary: dict[str, Any],
) -> str:
    if _int_value(r5_summary.get("paper_ready_candidates")) > 0:
        return PAPER_READY_CANDIDATE_AVAILABLE
    if not r5_guard.get("running") and r5_guard.get("status") in {
        "STOPPED",
        "STOPPED_WITH_STALE_PID",
        "NO_UNATTENDED_JOB",
    }:
        return WATCHER_NOT_RUNNING_OR_STALE
    if bool(r5_guard.get("stale_report")) and not bool(r5_guard.get("running")):
        return WATCHER_NOT_RUNNING_OR_STALE
    current_markets = max(
        _int_value(current_scope_summary.get("current_active_crypto_markets")),
        _int_value(current_scope_summary.get("current_active_crypto_markets_total")),
        _int_value(r5_summary.get("current_active_window_rows")),
        _int_value(r5_summary.get("active_pure_crypto_rows")),
    )
    if current_markets <= 0:
        return NO_CURRENT_ACTIVE_CRYPTO_MARKETS
    positive_ev = _int_value(r5_summary.get("positive_ev_rows"))
    primary_gap = str(r5_summary.get("primary_gap_after_refresh") or "")
    if (
        positive_ev <= 0
        and primary_gap == EV_NOT_POSITIVE
        and not _freshness_backlog_blocks_current_positive_ev(r5_summary)
    ):
        return EV_NOT_POSITIVE
    if (
        positive_ev > 0
        and primary_gap
        in {
            LOW_EDGE_OR_SCORE_BLOCK,
            "POSITIVE_EV_NO_EXECUTABLE_BOOK",
            RISK_OR_SIZE_BLOCK,
        }
        and not _freshness_backlog_blocks_current_positive_ev(r5_summary)
    ):
        if primary_gap == "POSITIVE_EV_NO_EXECUTABLE_BOOK":
            return LIQUIDITY_OR_SPREAD_BLOCK
        return primary_gap
    if _int_value(r5_summary.get("snapshot_stale_rows")) > 0 or _int_value(
        r5_summary.get("snapshot_missing_rows")
    ) > 0:
        return SNAPSHOT_STALE
    if _int_value(r5_summary.get("forecast_stale_rows")) > 0 or _int_value(
        r5_summary.get("forecast_missing_rows")
    ) > 0:
        return FORECAST_STALE
    if _ranking_gap(r5_summary) > 0:
        return RANKING_GAP
    if positive_ev <= 0 or primary_gap == EV_NOT_POSITIVE:
        return EV_NOT_POSITIVE
    if _int_value(r5_summary.get("positive_ev_no_executable_book_rows")) > 0:
        return LIQUIDITY_OR_SPREAD_BLOCK
    if _int_value(r5_summary.get("positive_ev_spread_blocked_rows")) > 0:
        return LIQUIDITY_OR_SPREAD_BLOCK
    if _int_value(r5_summary.get("positive_ev_clean_book_rows")) > 0 and _int_value(
        r5_summary.get("positive_ev_clean_book_risk_missing_rows")
    ) > 0:
        return RISK_OR_SIZE_BLOCK
    if str(r5_summary.get("liquidity_actionability_state") or "") in {
        "POSITIVE_EV_NO_EXECUTABLE_BOOK",
        "WAITING_FOR_EXECUTABLE_BOOK",
    }:
        return EXECUTABLE_EV_NOT_POSITIVE
    return PAPER_ORDER_CREATION_BLOCKED


def _current_crypto_funnel(
    *,
    r5_summary: dict[str, Any],
    r5_guard: dict[str, Any],
    current_scope_summary: dict[str, Any],
    true_blocker: str,
    stale_artifacts_ignored: int,
) -> dict[str, Any]:
    snapshot_stale = _int_value(r5_summary.get("snapshot_stale_rows"))
    forecast_stale = _int_value(r5_summary.get("forecast_stale_rows"))
    ranking_gap = _ranking_gap(r5_summary)
    runner_state = _r5_runner_state(r5_guard)
    current_active = max(
        _int_value(current_scope_summary.get("current_active_crypto_markets")),
        _int_value(current_scope_summary.get("current_active_crypto_markets_total")),
        _int_value(r5_summary.get("current_active_window_rows")),
        _int_value(r5_summary.get("active_pure_crypto_rows")),
    )
    return {
        "true_current_blocker": true_blocker,
        "current_active_crypto_markets": current_active,
        "current_snapshots": _int_value(current_scope_summary.get("current_snapshots")),
        "snapshots_fresh": snapshot_stale == 0 and _int_value(
            r5_summary.get("snapshot_missing_rows")
        ) == 0,
        "snapshot_stale_rows": snapshot_stale,
        "forecasts_fresh": forecast_stale == 0 and _int_value(
            r5_summary.get("forecast_missing_rows")
        ) == 0,
        "forecast_stale_rows": forecast_stale,
        "rankings_fresh": ranking_gap == 0,
        "ranking_gap_after_repair": ranking_gap,
        "current_positive_ev_rows": _int_value(r5_summary.get("positive_ev_rows")),
        "clean_execution_rows": _int_value(r5_summary.get("clean_execution_rows")),
        "paper_ready_candidates": _int_value(r5_summary.get("paper_ready_candidates")),
        "best_current_expected_value_cents": r5_summary.get(
            "best_current_expected_value_cents",
            "n/a",
        ),
        "best_ev_gap_to_positive_cents": r5_summary.get(
            "best_ev_gap_to_positive_cents",
            "n/a",
        ),
        "best_ev_candidate_ticker": r5_summary.get("best_ev_candidate_ticker") or "n/a",
        "r5_running": bool(r5_guard.get("running")),
        "r5_status": r5_guard.get("status") or "UNKNOWN",
        "r5_runner_state": runner_state,
        "r5_stale_report": bool(r5_guard.get("stale_report")),
        "r5_latest_age_seconds": _first_present(
            r5_guard,
            "latest_age_seconds",
            "latest_report_age_seconds",
            "age_seconds",
        ),
        "r5_freshness_window_minutes": _first_present(
            r5_guard,
            "freshness_window_minutes",
            "stale_after_minutes",
        ),
        "watch_state": r5_summary.get("watch_state") or "UNKNOWN",
        "primary_gap_after_refresh": r5_summary.get("primary_gap_after_refresh")
        or "UNKNOWN",
        "phase3bc_main_blocker": r5_summary.get("phase3bc_main_blocker") or "UNKNOWN",
        "data_freshness_gap_after_refresh": r5_summary.get(
            "data_freshness_gap_after_refresh"
        )
        or "UNKNOWN",
        "snapshot_backlog_status": r5_summary.get("snapshot_backlog_status")
        or "UNKNOWN",
        "forecast_backlog_status": r5_summary.get("forecast_backlog_status")
        or "UNKNOWN",
        "data_freshness_complete": bool(r5_summary.get("data_freshness_complete")),
        "data_freshness_partial_reason": r5_summary.get(
            "data_freshness_partial_reason"
        ),
        "stale_artifacts_ignored": stale_artifacts_ignored,
    }


def _r5_runner_state(r5_guard: dict[str, Any]) -> str:
    if bool(r5_guard.get("running")) and bool(r5_guard.get("stale_report")):
        return RUNNING_CYCLE_OVERDUE
    if bool(r5_guard.get("running")):
        return "RUNNING"
    if str(r5_guard.get("status") or "") in {
        "STOPPED",
        "STOPPED_WITH_STALE_PID",
        "NO_UNATTENDED_JOB",
    }:
        return WATCHER_NOT_RUNNING_OR_STALE
    return str(r5_guard.get("status") or "UNKNOWN")


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _freshness_backlog_blocks_current_positive_ev(
    r5_summary: dict[str, Any],
) -> bool:
    if bool(r5_summary.get("freshness_backlog_blocks_current_positive_ev")):
        return True
    return _int_value(r5_summary.get("positive_ev_snapshot_stale_rows")) > 0 or _int_value(
        r5_summary.get("positive_ev_forecast_stale_rows")
    ) > 0


def _artifact_audit_row(
    *,
    reports_dir: Path,
    name: str,
    relative_path: Path,
    trusted_source: bool,
    r5_summary: dict[str, Any],
    r5_cycle_at: Any,
    generated_at: Any,
    stale_after_minutes: int,
) -> dict[str, Any]:
    path = reports_dir / relative_path
    payload = _read_json(path)
    if not payload:
        return {
            "name": name,
            "path": str(path),
            "classification": MISSING,
            "generated_at": None,
            "age_minutes": None,
            "reason": "Artifact does not exist or is not valid JSON.",
        }
    artifact_at = _artifact_generated_at(payload)
    age_minutes = _age_minutes(artifact_at, generated_at)
    conflict_reason = _artifact_conflict_reason(
        name=name,
        payload=payload,
        r5_summary=r5_summary,
    )
    if conflict_reason:
        classification = CONFLICTS_WITH_R5
        reason = conflict_reason
    elif artifact_at is not None and age_minutes > stale_after_minutes:
        classification = STALE_ARTIFACT
        reason = f"Artifact is older than {stale_after_minutes} minutes."
    elif (
        artifact_at is not None
        and r5_cycle_at is not None
        and artifact_at < r5_cycle_at
        and not trusted_source
    ):
        classification = STALE_ARTIFACT
        reason = "Artifact predates the latest trusted R5 cycle."
    elif trusted_source:
        classification = CURRENT_AND_TRUSTED
        reason = "Trusted current-source artifact."
    else:
        classification = CURRENT_BUT_DIAGNOSTIC_ONLY
        reason = "Useful diagnostics, but not allowed to drive the primary blocker."
    return {
        "name": name,
        "path": str(path),
        "classification": classification,
        "generated_at": artifact_at.isoformat() if artifact_at else None,
        "age_minutes": round(age_minutes, 3) if artifact_at is not None else None,
        "reason": reason,
        "positive_ev_rows": _summary_int(payload, "positive_ev_rows"),
        "paper_ready_rows": _summary_int(payload, "paper_ready_rows"),
        "first_hard_blocker": _summary_value(payload, "first_hard_blocker"),
    }


def _artifact_conflict_reason(
    *,
    name: str,
    payload: dict[str, Any],
    r5_summary: dict[str, Any],
) -> str | None:
    lower_name = name.lower()
    if "phase 3ar" not in lower_name:
        return None
    artifact_positive = _summary_int(payload, "positive_ev_rows")
    artifact_ready = _summary_int(payload, "paper_ready_rows")
    r5_positive = _int_value(r5_summary.get("positive_ev_rows"))
    r5_ready = _int_value(r5_summary.get("paper_ready_candidates"))
    if artifact_ready > 0 and r5_ready == 0:
        return "Phase 3AR claims paper-ready rows that current R5 truth does not confirm."
    if artifact_positive > 0 and artifact_positive != r5_positive:
        return (
            "Phase 3AR positive-EV rows do not match current R5 positive-EV rows; "
            "treat as historical URL repair evidence."
        )
    if (
        artifact_positive > 0
        and str(r5_summary.get("primary_gap_after_refresh") or "") == EV_NOT_POSITIVE
    ):
        return "Phase 3AR positive-EV evidence conflicts with R5 EV_NOT_POSITIVE."
    return None


def _ui_panel(
    *,
    current_funnel: dict[str, Any],
    true_blocker: str,
    stale_artifacts_ignored: int,
    next_command: str,
    r5_status_path: Path,
) -> dict[str, Any]:
    status_label = _ui_status_label(true_blocker, current_funnel)
    status_kind = _ui_status_kind(true_blocker)
    evidence = (
        f"best_current_expected_value_cents={current_funnel['best_current_expected_value_cents']}, "
        f"positive_ev_rows={current_funnel['current_positive_ev_rows']}, "
        f"clean_execution_rows={current_funnel['clean_execution_rows']}"
    )
    runner_status_kind = "healthy" if current_funnel["r5_running"] else "stale"
    if current_funnel["r5_runner_state"] == RUNNING_CYCLE_OVERDUE:
        runner_status_kind = "warn"
    runner_label = _display_runner_label(current_funnel)
    blockers = [
        {
            "area": "Current crypto truth",
            "source": "Phase 3AW / trusted R5 current-window resolver",
            "status": true_blocker,
            "status_kind": status_kind if status_kind != "good" else "healthy",
            "status_label": status_label,
            "evidence": evidence,
            "next_action": _next_action_text(true_blocker),
        },
        {
            "area": "Watcher freshness",
            "source": "Phase 3AX-R9 guarded refresh status",
            "status": current_funnel["r5_runner_state"],
            "status_kind": runner_status_kind,
            "status_label": runner_label,
            "evidence": (
                f"Runner {runner_label}; "
                f"watch state {_format_enum(str(current_funnel['watch_state']))}; "
                f"latest_age_seconds={current_funnel['r5_latest_age_seconds']}; "
                f"freshness_window_minutes={current_funnel['r5_freshness_window_minutes']}."
            ),
            "next_action": (
                "Use the R9 status-only command for refresh-job truth; do not start "
                "a duplicate watcher."
            ),
        },
    ]
    if stale_artifacts_ignored:
        blockers.append(
            {
                "area": "Phase 3AR old artifact ignored",
                "source": "Phase 3AW artifact audit",
                "status": STALE_ARTIFACT,
                "status_kind": "warn",
                "status_label": "Old Artifact Ignored",
                "evidence": (
                    f"{stale_artifacts_ignored} old or conflicting artifact(s) were "
                    "kept out of the primary blocker calculation."
                ),
                "next_action": (
                    "Use Phase 3AR only as historical URL repair evidence unless "
                    "current R5 positive-EV rows require URL checks."
                ),
            }
        )
    return {
        "summary": _summary_text(true_blocker, current_funnel),
        "status_kind": status_kind,
        "status_label": status_label,
        "metrics": [
            {"label": "Paper-ready", "value": current_funnel["paper_ready_candidates"]},
            {"label": "Positive EV", "value": current_funnel["current_positive_ev_rows"]},
            {
                "label": "Clean execution rows",
                "value": current_funnel["clean_execution_rows"],
            },
            {
                "label": "Best EV",
                "value": _format_cents(current_funnel["best_current_expected_value_cents"]),
            },
            {
                "label": "Gap to positive",
                "value": _format_cents(current_funnel["best_ev_gap_to_positive_cents"]),
            },
            {
                "label": "R5 status",
                "value": runner_label,
            },
            {
                "label": "R9 refresh source",
                "value": "Phase 3AX-R9 guarded refresh job",
            },
            {"label": "Old artifacts ignored", "value": stale_artifacts_ignored},
        ],
        "last_updated": str(current_funnel.get("watch_state") or "n/a"),
        "blockers": blockers,
        "positive_ev_rows": [],
        "report_links": [
            {"label": "Truth report", "href": "/reports/phase3aw/EXECUTIVE_SUMMARY.md"},
            {"label": "R9 status", "href": "/reports/phase3ax_r9/guarded_refresh_job.json"},
            {"label": "R5 status", "href": f"/{r5_status_path.as_posix()}"},
            {"label": "Artifact audit", "href": "/reports/phase3aw/stale_artifact_audit.json"},
            {"label": "Current funnel", "href": "/reports/phase3aw/current_crypto_funnel.md"},
        ],
        "operator_next_command": next_command,
    }


def _display_runner_label(funnel: dict[str, Any]) -> str:
    if funnel.get("r5_runner_state") == RUNNING_CYCLE_OVERDUE:
        return "Refresh running / cycle overdue"
    return _format_enum(str(funnel.get("r5_status") or funnel.get("r5_runner_state")))


def _render_current_crypto_funnel(payload: dict[str, Any]) -> str:
    funnel = payload["current_crypto_funnel"]
    lines = [
        "# Phase 3AW Current Crypto Funnel",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- True blocker: `{funnel['true_current_blocker']}`",
        f"- R5 runner state: `{funnel['r5_runner_state']}`",
        f"- R5 stale report: `{funnel['r5_stale_report']}`",
        f"- Active current crypto markets: `{funnel['current_active_crypto_markets']}`",
        f"- Snapshots fresh: `{funnel['snapshots_fresh']}`",
        f"- Forecasts fresh: `{funnel['forecasts_fresh']}`",
        f"- Rankings fresh: `{funnel['rankings_fresh']}`",
        f"- Current positive-EV rows: `{funnel['current_positive_ev_rows']}`",
        f"- Clean execution rows: `{funnel['clean_execution_rows']}`",
        f"- Paper-ready candidates: `{funnel['paper_ready_candidates']}`",
        f"- Best EV cents: `{funnel['best_current_expected_value_cents']}`",
        f"- Gap to positive cents: `{funnel['best_ev_gap_to_positive_cents']}`",
        "",
        "No paper, demo, or live exchange writes are performed by this report.",
        "",
    ]
    return "\n".join(lines)


def _render_dashboard_truth_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    audit = payload["stale_artifact_audit"]
    counts = audit.get("classification_counts", {})
    lines = [
        "# Phase 3AW Dashboard Truth Reconciliation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Git commit: `{payload['metadata']['git_commit']}`",
        f"- DB fingerprint: `{payload['metadata']['db_fingerprint']}`",
        f"- Data watermark: `{json.dumps(payload['metadata']['data_watermark'], sort_keys=True)}`",
        "- Safety: paper/read-only; live/demo execution blocked; order submission blocked.",
        "",
        "## Answers",
        "",
        f"1. Bot state: `{summary['bot_state']}`.",
        f"2. True current blocker: `{summary['true_current_blocker']}`.",
        f"3. R5 running: `{summary['r5_running']}` ({summary['r5_guard_status']}).",
        f"3a. R5 runner state: `{summary['r5_runner_state']}`.",
        f"4. Snapshots fresh: `{summary['snapshots_fresh']}`.",
        f"5. Forecasts fresh: `{summary['forecasts_fresh']}`.",
        f"6. Rankings fresh: `{summary['rankings_fresh']}`.",
        f"7. Current positive-EV rows: `{summary['current_positive_ev_rows']}`.",
        f"8. Clean execution rows: `{summary['clean_execution_rows']}`.",
        f"9. Paper-ready candidates: `{summary['paper_ready_candidates']}`.",
        (
            "10. Stale/misleading UI rows: "
            f"`{summary['stale_artifacts_ignored']}` ignored; classifications "
            f"`{json.dumps(counts, sort_keys=True)}`."
        ),
        f"11. Exact next command: `{summary['operator_next_command']}`.",
        (
            "12. Do not run: force paper trades, threshold-lowering commands, "
            "or live/demo exchange writes."
        ),
        "",
    ]
    return "\n".join(lines)


def _render_dashboard_next_actions(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Phase 3AW Next Actions",
            "",
            "Registered command only:",
            "",
            "```bash",
            payload["operator_next_command"],
            "```",
            "",
            "Do not force paper trades or lower thresholds.",
            "",
        ]
    )


def _render_operator_next_command(payload: dict[str, Any]) -> str:
    if payload["summary"]["true_current_blocker"] == EV_NOT_POSITIVE:
        return "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "# No repair command needed.",
                "# The bot is correctly waiting for positive expected value.",
                payload["operator_next_command"],
                "",
            ]
        )
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            payload["operator_next_command"],
            "",
        ]
    )


def _dashboard_metadata(
    session: Session,
    *,
    settings: Settings,
    command_args: list[str],
    generated_at: Any,
    reports_dir: Path,
    r5_status: dict[str, Any],
    r5_watch: dict[str, Any],
    phase3at_diagnostic: dict[str, Any],
) -> dict[str, Any]:
    db_url = _database_url(session)
    return {
        "generated_at": generated_at.isoformat(),
        "git_commit": _git_commit(),
        "db_fingerprint": _db_fingerprint(session, db_url),
        "db_url_redacted": db_url,
        "command_args": command_args,
        "data_watermark": _data_watermark(
            reports_dir=reports_dir,
            r5_status=r5_status,
            r5_watch=r5_watch,
            phase3at_diagnostic=phase3at_diagnostic,
        ),
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
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _data_watermark(
    *,
    reports_dir: Path,
    r5_status: dict[str, Any],
    r5_watch: dict[str, Any],
    phase3at_diagnostic: dict[str, Any],
) -> dict[str, Any]:
    r3_refresh = _read_json(
        reports_dir / "phase3bc_r3" / "phase3bc_r3_active_crypto_refresh.json"
    )
    r7_ranking = _read_json(
        reports_dir / "phase3bc_r7" / "phase3bc_r7_crypto_ranking_coverage_repair.json"
    )
    phase3bc_router = _read_json(
        reports_dir / "phase3bc" / "phase3bc_crypto_clean_opportunity_router.json"
    )
    return {
        "source": "trusted_artifacts_no_db_table_scan",
        "r5_status_generated_at": _iso_or_none(_artifact_generated_at(r5_status)),
        "r5_watch_generated_at": _iso_or_none(_artifact_generated_at(r5_watch)),
        "r3_refresh_generated_at": _iso_or_none(_artifact_generated_at(r3_refresh)),
        "r7_ranking_generated_at": _iso_or_none(_artifact_generated_at(r7_ranking)),
        "phase3at_diagnostic_generated_at": _iso_or_none(
            _artifact_generated_at(phase3at_diagnostic)
        ),
        "phase3bc_router_generated_at": _iso_or_none(
            _artifact_generated_at(phase3bc_router)
        ),
        "market_snapshot_latest_evidence_at": _first_watermark(
            r5_watch,
            r3_refresh,
            "market_snapshot_max_captured_at",
            "snapshot_max_captured_at",
            "latest_snapshot_captured_at",
            "generated_at",
        ),
        "forecast_latest_evidence_at": _first_watermark(
            r5_watch,
            phase3at_diagnostic,
            "forecast_max_forecasted_at",
            "latest_forecast_at",
            "generated_at",
        ),
        "ranking_latest_evidence_at": _first_watermark(
            r5_watch,
            r7_ranking,
            "ranking_max_ranked_at",
            "latest_ranking_at",
            "generated_at",
        ),
    }


def _first_watermark(*payloads_and_keys: Any) -> str | None:
    payloads: list[dict[str, Any]] = []
    keys: list[str] = []
    for item in payloads_and_keys:
        if isinstance(item, dict):
            payloads.append(item)
        elif isinstance(item, str):
            keys.append(item)
    for payload in payloads:
        for key in keys:
            value = _summary_value(payload, key)
            if value not in {None, "", "UNKNOWN", "n/a"}:
                parsed = parse_datetime(value)
                return _iso_or_none(parsed) or str(value)
    return None


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


def _artifact_generated_at(payload: dict[str, Any]) -> Any:
    candidates: list[Any] = [
        payload.get("generated_at"),
        payload.get("latest_report_generated_at"),
    ]
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    candidates.append(metadata.get("generated_at"))
    for candidate in candidates:
        parsed = parse_datetime(candidate)
        if parsed is not None:
            return parsed
    return None


def _age_minutes(value: Any, now: Any) -> float:
    if value is None:
        return 0.0
    return max(0.0, (now - value).total_seconds() / 60)


def _ranking_gap(summary: dict[str, Any]) -> int:
    for key in ("ranking_coverage_gap_after_repair", "true_ranking_gap_after_repair"):
        if key in summary:
            return _int_value(summary.get(key))
    return (
        _int_value(summary.get("ranking_missing_rows"))
        + _int_value(summary.get("ranking_stale_rows"))
        + _int_value(summary.get("ranking_before_forecast_rows"))
    )


def _summary_value(payload: dict[str, Any], key: str) -> Any:
    for source_key in ("summary", "latest_summary", "gate_summary"):
        source = payload.get(source_key)
        if isinstance(source, dict) and key in source:
            return source.get(key)
    if key in payload:
        return payload.get(key)
    return None


def _summary_int(payload: dict[str, Any], key: str) -> int:
    return _int_value(_summary_value(payload, key))


def _classification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("classification") or UNKNOWN)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _operator_next_command(true_blocker: str) -> str:
    guarded_status_command = (
        "kalshi-bot phase3ax-r9-guarded-refresh-job "
        "--output-dir reports/phase3ax_r9 --reports-dir reports "
        "--r5-output-dir reports/phase3bc_r5 --status-only"
    )
    if true_blocker == EV_NOT_POSITIVE:
        return guarded_status_command
    if true_blocker in {
        WATCHER_NOT_RUNNING_OR_STALE,
        SNAPSHOT_STALE,
        FORECAST_STALE,
        RANKING_GAP,
        NO_CURRENT_ACTIVE_CRYPTO_MARKETS,
    }:
        return guarded_status_command
    return (
        "kalshi-bot phase3aw-dashboard-truth "
        "--output-dir reports/phase3aw --reports-dir reports"
    )


def _bot_state(true_blocker: str) -> str:
    if true_blocker == EV_NOT_POSITIVE:
        return "CORRECTLY_WAITING_FOR_POSITIVE_EV"
    if true_blocker == PAPER_READY_CANDIDATE_AVAILABLE:
        return "PAPER_READY_CANDIDATE_AVAILABLE_READ_ONLY"
    if true_blocker in {
        WATCHER_NOT_RUNNING_OR_STALE,
        SNAPSHOT_STALE,
        FORECAST_STALE,
        RANKING_GAP,
    }:
        return "NEEDS_DATA_FRESHNESS_ATTENTION"
    return "NO_PAPER_TRADE_CORRECT_UNTIL_GATE_CLEARS"


def _blocker_label(blocker: str) -> str:
    labels = {
        EV_NOT_POSITIVE: "Waiting for positive EV",
        SNAPSHOT_STALE: "Refreshing snapshots",
        FORECAST_STALE: "Refreshing forecasts",
        RANKING_GAP: "Ranking gap",
        WATCHER_NOT_RUNNING_OR_STALE: "Watcher needs attention",
        NO_CURRENT_ACTIVE_CRYPTO_MARKETS: "No current active crypto markets",
        EXECUTABLE_EV_NOT_POSITIVE: "Executable EV not positive",
        LIQUIDITY_OR_SPREAD_BLOCK: "Liquidity or spread block",
        LOW_EDGE_OR_SCORE_BLOCK: "Edge or score below threshold",
        URL_OR_CATALOG_BLOCK: "URL or catalog block",
        RISK_OR_SIZE_BLOCK: "Risk or size block",
        PAPER_READY_CANDIDATE_AVAILABLE: "Paper-ready candidate available",
        PAPER_ORDER_CREATION_BLOCKED: "Paper order creation blocked",
    }
    return labels.get(blocker, _format_enum(blocker))


def _display_blocker_label(blocker: str, funnel: dict[str, Any]) -> str:
    if (
        blocker == WATCHER_NOT_RUNNING_OR_STALE
        and funnel.get("r5_runner_state") == RUNNING_CYCLE_OVERDUE
    ):
        return "Refresh running / cycle overdue"
    if blocker == SNAPSHOT_STALE and funnel.get("r5_running"):
        return "Refreshing snapshots"
    if blocker == FORECAST_STALE and funnel.get("r5_running"):
        return "Refreshing forecasts"
    return _blocker_label(blocker)


def _ui_status_label(blocker: str, funnel: dict[str, Any]) -> str:
    if blocker == EV_NOT_POSITIVE:
        return "Waiting for Positive EV"
    return _display_blocker_label(blocker, funnel)


def _ui_status_kind(blocker: str) -> str:
    if blocker == PAPER_READY_CANDIDATE_AVAILABLE:
        return "good"
    if blocker in {WATCHER_NOT_RUNNING_OR_STALE, SNAPSHOT_STALE, FORECAST_STALE, RANKING_GAP}:
        return "warn"
    if blocker == EV_NOT_POSITIVE:
        return "neutral"
    return "warn"


def _summary_text(true_blocker: str, funnel: dict[str, Any]) -> str:
    if true_blocker == EV_NOT_POSITIVE:
        text = (
            "The crypto watch is running. Current snapshots, forecasts, and rankings "
            "are healthy. No current crypto market has strictly positive expected "
            "value, so no paper trade should be created."
        )
        if funnel.get("r5_runner_state") == RUNNING_CYCLE_OVERDUE:
            return (
                "The guarded R5 refresh job is active, but its latest cycle is overdue. "
                "Current R5 truth still points to EV_NOT_POSITIVE, so old stale "
                "artifacts must not create a dashboard blocker."
            )
        return text
    if true_blocker == WATCHER_NOT_RUNNING_OR_STALE:
        return "The R5 watch needs attention; refresh status before trusting paper readiness."
    if true_blocker in {SNAPSHOT_STALE, FORECAST_STALE, RANKING_GAP}:
        return "Current crypto evidence is still catching up, so paper readiness is blocked."
    if true_blocker == PAPER_READY_CANDIDATE_AVAILABLE:
        return "A paper-ready candidate exists, but this dashboard remains read-only."
    return (
        f"Current crypto truth is blocked at {_blocker_label(true_blocker)} with "
        f"{funnel['current_positive_ev_rows']} positive-EV row(s)."
    )


def _next_action_text(true_blocker: str) -> str:
    if true_blocker == EV_NOT_POSITIVE:
        return "Keep the R9 guarded R5 refresh job running. Do not force paper trades."
    if true_blocker in {SNAPSHOT_STALE, FORECAST_STALE, RANKING_GAP}:
        return (
            "Keep the bounded R5 refresh/watch path active; do not use old URL "
            "artifacts as primary evidence."
        )
    if true_blocker == WATCHER_NOT_RUNNING_OR_STALE:
        return (
            "Run the R5 status command and restart only the watcher if the guard "
            "says it is stopped."
        )
    return "Review current R5 truth before taking any paper-only follow-up."


def _format_enum(value: str) -> str:
    text = str(value or UNKNOWN).replace("_", " ").strip()
    return text.title() if text else UNKNOWN


def _format_cents(value: Any) -> str:
    if value in {None, "", "n/a"}:
        return "n/a"
    try:
        return f"{float(value):.1f}c"
    except (TypeError, ValueError):
        return str(value)


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _int_value(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines: list[str] = []
    for artifact in files:
        if not artifact.exists():
            continue
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_phase3aw_recovery_status(
    *,
    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR,
    stale_after_seconds: int = 300,
    monitor_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    heartbeat_status = load_latest_long_job_status(
        output_dir=heartbeat_dir,
        stale_after_seconds=stale_after_seconds,
    )
    monitor = monitor_payload or db_writer_monitor()
    heartbeat = heartbeat_status.get("heartbeat") or {}
    checkpoint = heartbeat_status.get("checkpoint") or {}
    events = _read_recent_events(heartbeat_dir / DEFAULT_EVENTS_FILE)
    classification = _classify(
        heartbeat=heartbeat,
        heartbeat_status=heartbeat_status,
        monitor=monitor,
    )
    safe_to_resume = _safe_to_resume(classification, monitor)
    resume_command = _resume_command(heartbeat)
    return {
        "phase": "3AW",
        "generated_at": utc_now().isoformat(),
        "mode": "PAPER ONLY diagnostics",
        "live_demo_execution": "blocked",
        "classification": classification,
        "safe_to_resume": safe_to_resume,
        "writer": {
            "pid": monitor.get("current_writer_pid"),
            "command": monitor.get("current_writer_command"),
            "elapsed": monitor.get("current_writer_elapsed"),
            "safe_to_start_write": monitor.get("safe_to_start_write"),
        },
        "heartbeat": heartbeat,
        "checkpoint": checkpoint,
        "heartbeat_age_seconds": heartbeat_status.get("heartbeat_age_seconds"),
        "heartbeat_age": heartbeat_status.get("heartbeat_age"),
        "event_count_sampled": len(events),
        "recent_events": events,
        "last_stage": heartbeat.get("stage") or checkpoint.get("stage"),
        "last_processed": heartbeat.get("processed") or checkpoint.get("processed"),
        "last_total": heartbeat.get("total") or checkpoint.get("total"),
        "last_item": heartbeat.get("current_item") or checkpoint.get("current_item"),
        "last_heartbeat_at": heartbeat.get("heartbeat_at"),
        "last_checkpoint_at": checkpoint.get("heartbeat_at"),
        "resume_command": resume_command,
        "recommended_next_action": _recommended_action(
            classification=classification,
            safe_to_resume=safe_to_resume,
            resume_command=resume_command,
            monitor=monitor,
        ),
    }


def write_phase3aw_recovery_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    heartbeat_dir: Path = DEFAULT_HEARTBEAT_DIR,
    stale_after_seconds: int = 300,
    monitor_payload: dict[str, Any] | None = None,
) -> dict[str, Path]:
    status = build_phase3aw_recovery_status(
        heartbeat_dir=heartbeat_dir,
        stale_after_seconds=stale_after_seconds,
        monitor_payload=monitor_payload,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / DEFAULT_JSON
    markdown_path = output_dir / DEFAULT_MARKDOWN
    json_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(status), encoding="utf-8")
    return {"json_path": json_path, "markdown_path": markdown_path}


def _classify(
    *,
    heartbeat: dict[str, Any],
    heartbeat_status: dict[str, Any],
    monitor: dict[str, Any],
) -> str:
    writer_pid = monitor.get("current_writer_pid")
    heartbeat_pid = heartbeat.get("pid")
    stage = str(heartbeat.get("stage") or "").upper()
    heartbeat_state = str(heartbeat_status.get("status") or UNKNOWN).upper()

    if writer_pid and not heartbeat:
        return LEGACY_WRITER_ACTIVE
    if writer_pid and _same_pid(heartbeat_pid, writer_pid) and heartbeat_state == "ACTIVE":
        return RUNNING
    if writer_pid and not _same_pid(heartbeat_pid, writer_pid):
        return LEGACY_WRITER_ACTIVE
    if writer_pid and heartbeat_state == "STALE":
        return HEARTBEAT_STALLED
    if stage == "COMPLETE":
        return COMPLETED_CLEANLY
    if stage == "STOPPED_EARLY":
        return STOPPED_EARLY
    if not heartbeat:
        return NO_HEARTBEAT
    if heartbeat_state == "STALE" and not writer_pid:
        return CRASHED_OR_INTERRUPTED
    return UNKNOWN


def _safe_to_resume(classification: str, monitor: dict[str, Any]) -> bool:
    if not monitor.get("safe_to_start_write"):
        return False
    return classification in {CRASHED_OR_INTERRUPTED, STOPPED_EARLY, NO_HEARTBEAT, UNKNOWN}


def _same_pid(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _resume_command(heartbeat: dict[str, Any]) -> str:
    job_name = str(heartbeat.get("job_name") or "link-remediate")
    if job_name == "link-remediate":
        return (
            "kalshi-bot link-remediate --resume --progress-every 100 "
            "--checkpoint-every 100 --stop-after-minutes 30"
        )
    return "kalshi-bot db-writer-monitor"


def _recommended_action(
    *,
    classification: str,
    safe_to_resume: bool,
    resume_command: str,
    monitor: dict[str, Any],
) -> str:
    if classification == RUNNING:
        return "Long job is running and heartbeat is fresh. Wait and monitor phase3au-status."
    if classification == HEARTBEAT_STALLED:
        return "Writer is active but heartbeat is stale. Wait briefly, then recheck before acting."
    if classification == LEGACY_WRITER_ACTIVE:
        return "A writer is active without Phase 3AU heartbeat. Do not start another writer."
    if classification == COMPLETED_CLEANLY:
        return "Long job completed cleanly. Run the next queued read/report command."
    if safe_to_resume:
        return f"Safe to resume after confirming no writer is active: {resume_command}"
    if not monitor.get("safe_to_start_write"):
        return "Another writer is active. Do not resume yet."
    return "Review heartbeat and checkpoint files before resuming."


def _render_markdown(status: dict[str, Any]) -> str:
    writer = status["writer"]
    heartbeat = status.get("heartbeat") or {}
    checkpoint = status.get("checkpoint") or {}
    recent_events = status.get("recent_events") or []
    lines = [
        "# Phase 3AW Long Job Crash Recovery Report",
        "",
        f"- Generated at: {status['generated_at']}",
        "- Mode: PAPER ONLY diagnostics",
        "- Live/demo execution: blocked",
        "",
        "## Recovery Status",
        "",
        f"- Classification: {status['classification']}",
        f"- Safe to resume: {'YES' if status['safe_to_resume'] else 'NO'}",
        f"- Recommended action: {status['recommended_next_action']}",
        "",
        "## Active Writer",
        "",
        f"- PID: {writer.get('pid') or 'none'}",
        f"- Command: `{writer.get('command') or 'none'}`",
        f"- Elapsed: {writer.get('elapsed') or 'n/a'}",
        f"- Safe to start write: {'YES' if writer.get('safe_to_start_write') else 'NO'}",
        "",
        "## Last Heartbeat",
        "",
        f"- Job: {heartbeat.get('job_name') or 'none'}",
        f"- PID: {heartbeat.get('pid') or 'none'}",
        f"- Stage: {heartbeat.get('stage') or 'none'}",
        f"- Processed: {heartbeat.get('processed') or 0} / {heartbeat.get('total') or 'unknown'}",
        f"- Current item: {heartbeat.get('current_item') or 'none'}",
        f"- Heartbeat at: {heartbeat.get('heartbeat_at') or 'none'}",
        f"- Heartbeat age: {status.get('heartbeat_age') or 'n/a'}",
        "",
        "## Last Checkpoint",
        "",
        f"- Stage: {checkpoint.get('stage') or 'none'}",
        f"- Processed: {checkpoint.get('processed') or 0} / {checkpoint.get('total') or 'unknown'}",
        f"- Checkpoint at: {checkpoint.get('heartbeat_at') or 'none'}",
        "",
        "## Resume Command",
        "",
        "```bash",
        status["resume_command"],
        "```",
        "",
        "## Recent Events",
        "",
    ]
    if recent_events:
        lines.extend(
            [
                "| Time | Stage | Processed | Item | Message |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for event in recent_events[-10:]:
            lines.append(
                "| "
                f"{event.get('heartbeat_at') or ''} | "
                f"{event.get('stage') or ''} | "
                f"{event.get('processed') or 0}/{event.get('total') or 'unknown'} | "
                f"`{event.get('current_item') or ''}` | "
                f"{event.get('message') or ''} |"
            )
    else:
        lines.append("No recent heartbeat events were found.")
    return "\n".join(lines) + "\n"


def _read_recent_events(path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def heartbeat_age_label(heartbeat_at: str | None) -> str | None:
    parsed = parse_datetime(heartbeat_at) if heartbeat_at else None
    if parsed is None:
        return None
    return format_elapsed(max(0, int((utc_now() - parsed).total_seconds())))
