from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.phase3aw import write_phase3aw_dashboard_truth_report
from kalshi_predictor.phase3ax import write_phase3ax_gap_analysis_report
from kalshi_predictor.phase3bc_r6 import (
    DEFAULT_CRYPTO_LINK_SCAN_LIMIT,
    DEFAULT_CRYPTO_MARKET_SCAN_LIMIT,
    DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    DEFAULT_CRYPTO_SERIES_TICKERS,
    DEFAULT_CRYPTO_SYMBOLS,
    DEFAULT_MARKET_PAGE_LIMIT,
    DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
    start_phase3bc_r5_unattended_watch,
    write_phase3bc_r5_status_report,
    write_phase3bc_r5_unattended_guard_report,
)
from kalshi_predictor.utils.time import utc_now

PHASE3AX_R9_VERSION = "phase3ax_r9_guarded_refresh_job_v1"
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_EXCHANGE_WRITES"
R5_WATCH_COMMAND_MARKER = "phase3bc-r5-crypto-freshness-watch"


@dataclass(frozen=True)
class Phase3AXR9ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    executive_summary_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3ax_r9_guarded_refresh_job_report(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ax_r9"),
    reports_dir: Path = Path("reports"),
    r5_output_dir: Path = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Path = Path("reports/phase3bc_r7"),
    settings: Settings | None = None,
    registered_commands: set[str] | None = None,
    command_args: list[str] | None = None,
    start_if_needed: bool = True,
    stop_overrun: bool = False,
    refresh_dashboard_truth: bool = True,
    refresh_gap_analysis: bool = True,
    stale_after_minutes: int = 120,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: str = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: str = "coinbase",
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
    ranking_repair_limit: int = 500,
    cycles: int = 32,
    interval_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    duration_hours: float = 8.0,
    timeout_grace_seconds: int = 900,
    near_money_per_symbol_limit: int = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: int = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: int = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
) -> Phase3AXR9ArtifactSet:
    """Write a one-command guarded refresh report and start R5 only when safe."""

    payload = build_phase3ax_r9_guarded_refresh_job(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        r5_output_dir=r5_output_dir,
        phase3bc_output_dir=phase3bc_output_dir,
        phase3bc_r3_output_dir=phase3bc_r3_output_dir,
        phase3bc_r4_output_dir=phase3bc_r4_output_dir,
        phase3bc_r7_output_dir=phase3bc_r7_output_dir,
        settings=settings,
        registered_commands=registered_commands,
        command_args=command_args,
        start_if_needed=start_if_needed,
        stop_overrun=stop_overrun,
        refresh_dashboard_truth=refresh_dashboard_truth,
        refresh_gap_analysis=refresh_gap_analysis,
        stale_after_minutes=stale_after_minutes,
        symbols=symbols,
        crypto_series_tickers=crypto_series_tickers,
        source=source,
        market_limit=market_limit,
        market_max_pages=market_max_pages,
        crypto_market_scan_limit=crypto_market_scan_limit,
        crypto_link_limit=crypto_link_limit,
        forecast_limit=forecast_limit,
        opportunity_limit=opportunity_limit,
        phase3bc_limit=phase3bc_limit,
        cadence_minutes=cadence_minutes,
        freshness_minutes=freshness_minutes,
        max_preflight=max_preflight,
        ranking_repair_limit=ranking_repair_limit,
        cycles=cycles,
        interval_minutes=interval_minutes,
        duration_hours=duration_hours,
        timeout_grace_seconds=timeout_grace_seconds,
        near_money_per_symbol_limit=near_money_per_symbol_limit,
        near_money_window_limit=near_money_window_limit,
        snapshot_fetch_concurrency=snapshot_fetch_concurrency,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "guarded_refresh_job.json"
    markdown_path = output_dir / "GUARDED_REFRESH_JOB.md"
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            json_path,
            markdown_path,
            executive_summary_path,
            next_actions_path,
        ],
    )
    return Phase3AXR9ArtifactSet(
        output_dir=output_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        executive_summary_path=executive_summary_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3ax_r9_guarded_refresh_job(
    session: Session,
    *,
    output_dir: Path = Path("reports/phase3ax_r9"),
    reports_dir: Path = Path("reports"),
    r5_output_dir: Path = Path("reports/phase3bc_r5"),
    phase3bc_output_dir: Path = Path("reports/phase3bc"),
    phase3bc_r3_output_dir: Path = Path("reports/phase3bc_r3"),
    phase3bc_r4_output_dir: Path = Path("reports/phase3bc_r4"),
    phase3bc_r7_output_dir: Path = Path("reports/phase3bc_r7"),
    settings: Settings | None = None,
    registered_commands: set[str] | None = None,
    command_args: list[str] | None = None,
    start_if_needed: bool = True,
    stop_overrun: bool = False,
    refresh_dashboard_truth: bool = True,
    refresh_gap_analysis: bool = True,
    stale_after_minutes: int = 120,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    crypto_series_tickers: str = DEFAULT_CRYPTO_SERIES_TICKERS,
    source: str = "coinbase",
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
    ranking_repair_limit: int = 500,
    cycles: int = 32,
    interval_minutes: int = DEFAULT_CRYPTO_REFRESH_CADENCE_MINUTES,
    duration_hours: float = 8.0,
    timeout_grace_seconds: int = 900,
    near_money_per_symbol_limit: int = DEFAULT_NEAR_MONEY_PER_SYMBOL_LIMIT,
    near_money_window_limit: int = DEFAULT_NEAR_MONEY_WINDOW_LIMIT,
    snapshot_fetch_concurrency: int = DEFAULT_SNAPSHOT_FETCH_CONCURRENCY,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    output_dir.mkdir(parents=True, exist_ok=True)
    command_args = command_args or []
    registered_commands = registered_commands or set()

    writer_before = db_writer_monitor(settings=resolved)
    status_before_artifacts = write_phase3bc_r5_status_report(output_dir=r5_output_dir)
    status_before = _read_json(status_before_artifacts.json_path)
    guard_artifacts = write_phase3bc_r5_unattended_guard_report(
        output_dir=r5_output_dir,
        stop_overrun=stop_overrun,
    )
    guard_payload = _read_json(guard_artifacts.json_path)
    guarded_status = guard_payload.get("after") or status_before
    guarded_guard = _guard(guarded_status)
    guarded_process = _process(guarded_status)

    start_result: dict[str, Any] | None = None
    start_attempted = False
    refused_reason = _start_refusal_reason(
        writer_status=writer_before,
        guarded_process=guarded_process,
        guarded_guard=guarded_guard,
        start_if_needed=start_if_needed,
    )
    if refused_reason is None:
        start_attempted = True
        start_result_obj = start_phase3bc_r5_unattended_watch(
            output_dir=r5_output_dir,
            phase3bc_output_dir=phase3bc_output_dir,
            phase3bc_r3_output_dir=phase3bc_r3_output_dir,
            phase3bc_r4_output_dir=phase3bc_r4_output_dir,
            phase3bc_r7_output_dir=phase3bc_r7_output_dir,
            symbols=symbols,
            crypto_series_tickers=crypto_series_tickers,
            source=source,
            market_limit=market_limit,
            market_max_pages=market_max_pages,
            crypto_market_scan_limit=crypto_market_scan_limit,
            crypto_link_limit=crypto_link_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
            phase3bc_limit=phase3bc_limit,
            cadence_minutes=cadence_minutes,
            freshness_minutes=freshness_minutes,
            max_preflight=max_preflight,
            ranking_repair_limit=ranking_repair_limit,
            cycles=cycles,
            interval_minutes=interval_minutes,
            duration_hours=duration_hours,
            timeout_grace_seconds=timeout_grace_seconds,
            near_money_per_symbol_limit=near_money_per_symbol_limit,
            near_money_window_limit=near_money_window_limit,
            snapshot_fetch_concurrency=snapshot_fetch_concurrency,
        )
        start_result = {
            "status": start_result_obj.status,
            "pid": start_result_obj.pid,
            "started": start_result_obj.started,
            "pid_path": str(start_result_obj.pid_path),
            "metadata_path": str(start_result_obj.metadata_path),
            "stdout_path": str(start_result_obj.stdout_path),
            "stderr_path": str(start_result_obj.stderr_path),
            "command": start_result_obj.command,
            "message": start_result_obj.message,
        }

    status_after_artifacts = write_phase3bc_r5_status_report(output_dir=r5_output_dir)
    status_after = _read_json(status_after_artifacts.json_path)
    writer_after = db_writer_monitor(settings=resolved)
    dashboard_artifacts = None
    if refresh_dashboard_truth:
        dashboard_artifacts = write_phase3aw_dashboard_truth_report(
            session,
            output_dir=reports_dir / "phase3aw",
            reports_dir=reports_dir,
            settings=resolved,
            command_args=command_args,
            stale_after_minutes=stale_after_minutes,
        )
    gap_artifacts = None
    if refresh_gap_analysis:
        gap_artifacts = write_phase3ax_gap_analysis_report(
            session,
            output_dir=reports_dir / "phase3ax",
            reports_dir=reports_dir,
            settings=resolved,
            command_args=command_args,
            registered_commands=registered_commands,
            db_writer_status=writer_after,
            stale_after_minutes=stale_after_minutes,
        )

    summary = _summary(
        status_after=status_after,
        start_attempted=start_attempted,
        start_result=start_result,
        refused_reason=refused_reason,
        dashboard_refreshed=dashboard_artifacts is not None,
        gap_refreshed=gap_artifacts is not None,
    )
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AX-R9",
        "phase_version": PHASE3AX_R9_VERSION,
        "mode": "PAPER_ONLY_GUARDED_REFRESH_SUPERVISOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "live_or_demo_execution": False,
        "order_submission": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
        "output_dir": str(output_dir),
        "reports_dir": str(reports_dir),
        "summary": summary,
        "writer_before": writer_before,
        "writer_after": writer_after,
        "r5_status_before": status_before,
        "r5_guard": guard_payload,
        "r5_status_after": status_after,
        "start_action": {
            "start_if_needed": start_if_needed,
            "start_attempted": start_attempted,
            "refused_reason": refused_reason,
            "result": start_result,
        },
        "dashboard_truth_artifacts": _artifact_paths(dashboard_artifacts),
        "gap_analysis_artifacts": _artifact_paths(gap_artifacts),
        "operator_next_action": _operator_next_action(summary),
        "next_codex_task": _next_codex_task(gap_artifacts),
        "safety_confirmation": {
            "paper_only": True,
            "live_demo_execution_blocked": True,
            "order_submit_cancel_replace_blocked": True,
            "paper_trade_creation_blocked": True,
            "thresholds_unchanged": True,
        },
    }


def _summary(
    *,
    status_after: dict[str, Any],
    start_attempted: bool,
    start_result: dict[str, Any] | None,
    refused_reason: str | None,
    dashboard_refreshed: bool,
    gap_refreshed: bool,
) -> dict[str, Any]:
    guard = _guard(status_after)
    process = _process(status_after)
    r5_running = bool(process.get("phase3bc_r5_process_running"))
    started = bool(start_result and start_result.get("started"))
    duplicate_refused = refused_reason == "R5_ALREADY_RUNNING"
    status = "RUNNING"
    if started:
        status = "STARTED"
    elif duplicate_refused:
        status = "ALREADY_RUNNING_NO_DUPLICATE_STARTED"
    elif refused_reason:
        status = f"NOT_STARTED_{refused_reason}"
    elif not r5_running:
        status = "NOT_RUNNING"
    latest_summary = status_after.get("latest_summary") or {}
    return {
        "status": status,
        "r5_running": r5_running,
        "r5_guard_status": guard.get("status"),
        "r5_pid": guard.get("pid") or _first_pid(process),
        "r5_stale_report": bool(guard.get("stale_report")),
        "r5_latest_age_seconds": guard.get("latest_age_seconds"),
        "r5_freshness_window_minutes": guard.get("freshness_window_minutes"),
        "start_attempted": start_attempted,
        "started": started,
        "duplicate_refused": duplicate_refused,
        "refused_reason": refused_reason,
        "watch_state": status_after.get("latest_watch_state"),
        "primary_gap_after_refresh": latest_summary.get("primary_gap_after_refresh"),
        "positive_ev_rows": latest_summary.get("positive_ev_rows"),
        "paper_ready_candidates": latest_summary.get("paper_ready_candidates"),
        "snapshot_stale_rows": latest_summary.get("snapshot_stale_rows"),
        "forecast_stale_rows": latest_summary.get("forecast_stale_rows"),
        "dashboard_truth_refreshed": dashboard_refreshed,
        "gap_analysis_refreshed": gap_refreshed,
    }


def _start_refusal_reason(
    *,
    writer_status: dict[str, Any],
    guarded_process: dict[str, Any],
    guarded_guard: dict[str, Any],
    start_if_needed: bool,
) -> str | None:
    if guarded_process.get("phase3bc_r5_process_running"):
        return "R5_ALREADY_RUNNING"
    if guarded_guard.get("status") == "OVERRUNNING":
        return "R5_OVERRUNNING_REQUIRES_GUARD_STOP"
    if _has_unrelated_writer(writer_status):
        return "UNRELATED_DB_WRITER_ACTIVE"
    if not start_if_needed:
        return "STATUS_ONLY"
    return None


def _has_unrelated_writer(writer_status: dict[str, Any]) -> bool:
    command = str(writer_status.get("current_writer_command") or "")
    if not command:
        return False
    if R5_WATCH_COMMAND_MARKER in command:
        return False
    return not bool(writer_status.get("safe_to_start_write", True))


def _operator_next_action(summary: dict[str, Any]) -> str:
    if summary.get("started"):
        return "Leave the guarded R5 refresh job running; use this R9 report as the status check."
    if summary.get("duplicate_refused"):
        return "A guarded R5 job is already running; do not start another watcher."
    reason = summary.get("refused_reason")
    if reason == "UNRELATED_DB_WRITER_ACTIVE":
        return "Wait for the active DB writer to finish, then rerun the R9 guarded refresh job."
    if reason == "R5_OVERRUNNING_REQUIRES_GUARD_STOP":
        return "Review the R5 guard report before stopping/restarting the overrun process."
    if reason == "STATUS_ONLY":
        return "Status-only run complete; rerun with --start-if-needed to let R9 start R5 safely."
    return "R9 report refreshed; review Phase 3AX for the next product phase."


def _next_codex_task(gap_artifacts: Any | None = None) -> dict[str, Any]:
    gap_path = getattr(gap_artifacts, "app_gap_analysis_json_path", None)
    if gap_path is not None:
        gap_payload = _read_json(Path(gap_path))
        task = gap_payload.get("next_codex_task")
        if isinstance(task, dict) and task.get("task_phase_name"):
            return task
    return {
        "task_phase_name": "Phase 3AX-R8 Dashboard Truth / Operator Workflow",
        "reason": (
            "R9 provides one guarded refresh entrypoint; the next roadmap step is "
            "making the UI/operator workflow consume one true status."
        ),
        "problem_statement": (
            "Route operator-facing UI, status, and next-action panels through the "
            "guarded refresh/dashboard truth evidence without stale artifacts."
        ),
        "acceptance_criteria": [
            "UI, Phase 3AW, Phase 3AX, and R9 agree on the same current blocker.",
            "No stale Phase 3AR/3AT/3AN artifact drives the primary blocker.",
            "Operator next actions reference registered commands only.",
            "No paper trades or exchange writes occur.",
        ],
    }


def _guard(payload: dict[str, Any]) -> dict[str, Any]:
    guard = payload.get("guard")
    return guard if isinstance(guard, dict) else {}


def _process(payload: dict[str, Any]) -> dict[str, Any]:
    process = payload.get("process")
    return process if isinstance(process, dict) else {}


def _first_pid(process: dict[str, Any]) -> int | None:
    pids = process.get("phase3bc_r5_pids")
    if not isinstance(pids, list) or not pids:
        return None
    try:
        return int(pids[0])
    except (TypeError, ValueError):
        return None


def _artifact_paths(artifacts: Any) -> dict[str, str]:
    if artifacts is None:
        return {}
    result: dict[str, str] = {}
    for key, value in vars(artifacts).items():
        if key.endswith("_path") and value is not None:
            result[key] = str(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    start = payload["start_action"]
    lines = [
        "# Phase 3AX-R9 Guarded Refresh Job",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Status: `{summary.get('status')}`",
        f"- R5 running: `{summary.get('r5_running')}`",
        f"- R5 guard status: `{summary.get('r5_guard_status')}`",
        f"- R5 PID: `{summary.get('r5_pid')}`",
        f"- R5 stale report: `{summary.get('r5_stale_report')}`",
        f"- R5 latest age seconds: `{summary.get('r5_latest_age_seconds')}`",
        f"- Start attempted: `{summary.get('start_attempted')}`",
        f"- Started: `{summary.get('started')}`",
        f"- Duplicate refused: `{summary.get('duplicate_refused')}`",
        f"- Refused reason: `{summary.get('refused_reason')}`",
        f"- Watch state: `{summary.get('watch_state')}`",
        f"- Primary gap after refresh: `{summary.get('primary_gap_after_refresh')}`",
        f"- Positive-EV rows: `{summary.get('positive_ev_rows')}`",
        f"- Paper-ready candidates: `{summary.get('paper_ready_candidates')}`",
        f"- Dashboard truth refreshed: `{summary.get('dashboard_truth_refreshed')}`",
        f"- Gap analysis refreshed: `{summary.get('gap_analysis_refreshed')}`",
        "",
        "## Start Action",
        "",
        f"- start_if_needed: `{start.get('start_if_needed')}`",
        f"- start_attempted: `{start.get('start_attempted')}`",
        f"- refused_reason: `{start.get('refused_reason')}`",
        f"- result: `{start.get('result')}`",
        "",
        "## Operator Next Action",
        "",
        payload["operator_next_action"],
        "",
        "## Safety",
        "",
        "- PAPER ONLY.",
        "- No live/demo submit, cancel, replace, or amend.",
        "- No paper trades created.",
        "- No threshold changes.",
        "",
    ]
    return "\n".join(lines)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AX-R9 Executive Summary",
        "",
        f"- Guarded refresh status: `{summary.get('status')}`",
        f"- R5 running: `{summary.get('r5_running')}`",
        f"- R5 PID: `{summary.get('r5_pid')}`",
        f"- Started this run: `{summary.get('started')}`",
        f"- Duplicate watcher refused: `{summary.get('duplicate_refused')}`",
        f"- Paper-ready candidates: `{summary.get('paper_ready_candidates')}`",
        f"- Operator next action: {payload['operator_next_action']}",
        "",
    ]
    return "\n".join(lines)


def _render_next_actions(payload: dict[str, Any]) -> str:
    next_task = payload["next_codex_task"]
    lines = [
        "# Phase 3AX-R9 Next Actions",
        "",
        "Operator:",
        "",
        payload["operator_next_action"],
        "",
        "Next Codex task:",
        "",
        f"`{next_task['task_phase_name']}`",
        "",
        next_task["problem_statement"],
        "",
        "Safety:",
        "",
        "- Keep PAPER / READ-ONLY unless the guarded R5 refresh itself is intentionally started.",
        "- Do not submit/cancel/replace/amend live or demo exchange orders.",
        "- Do not create paper trades from partial diagnostics.",
        "",
    ]
    return "\n".join(lines)


def _write_manifest(path: Path, files: list[Path]) -> None:
    lines = []
    for file_path in files:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {file_path.name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
