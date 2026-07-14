from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import ProbeRunner
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    DEFAULT_SERVICE_NAME,
)
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    DEFAULT_UI_TIMEOUT_SECONDS,
    UiApiProbeRunner,
    build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R33_VERSION = "phase3bb_r33_cloud_paper_only_operations_readiness_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r33")
VERIFIED_R32_STATUS = "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS"
PAPER_EXECUTION_MODE = "paper_shadow"


@dataclass(frozen=True)
class Phase3BBR33CloudPaperOnlyOperationsReadinessArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    checks_csv_path: Path
    warnings_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r33_cloud_paper_only_operations_readiness_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> Phase3BBR33CloudPaperOnlyOperationsReadinessArtifacts:
    payload = build_phase3bb_r33_cloud_paper_only_operations_readiness(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        scheduler_probe_runner=scheduler_probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_paper_only_operations_readiness.md"
    json_path = output_dir / "cloud_paper_only_operations_readiness.json"
    checks_csv_path = output_dir / "readiness_checks.csv"
    warnings_csv_path = output_dir / "readiness_warnings.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_rows_csv(checks_csv_path, payload["readiness_checks"])
    _write_rows_csv(warnings_csv_path, payload["readiness_warnings"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            checks_csv_path,
            warnings_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR33CloudPaperOnlyOperationsReadinessArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        checks_csv_path=checks_csv_path,
        warnings_csv_path=warnings_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r33_cloud_paper_only_operations_readiness(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    scheduler_probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness",
        "argv": command_args or [],
    }
    r32 = build_phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status(
        session,
        output_dir=output_dir / "r32_dashboard_scheduler_status",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification"],
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        scheduler_probe_runner=scheduler_probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    operations = _operations_snapshot(r32)
    checks = _readiness_checks(r32, operations)
    warnings = _readiness_warnings(operations)
    decision = _decision(checks, warnings, operations)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "operations_readiness_monitor_only": True,
        "remote_scheduler_read_only_commands": len(
            (r32.get("r18_scheduler_status") or {}).get("remote_probe_results") or []
        ),
        "ui_http_get_requests": len(r32.get("ui_dashboard_truth_probe_results") or []),
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "service_files_written_to_system": False,
        "starts_ui_service": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "nginx_or_firewall_changed": False,
        "public_exposure_changed": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "thresholds_lowered": False,
    }
    return {
        **metadata,
        "phase": "3BB-R33-CLOUD-PAPER-ONLY-OPERATIONS-READINESS",
        "phase_version": PHASE3BB_R33_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_OPERATIONS_READINESS_MONITOR",
        "reports_dir": str(reports_dir),
        "r32_dashboard_scheduler_status": r32,
        "operations_snapshot": operations,
        "readiness_checks": checks,
        "readiness_warnings": warnings,
        "readiness_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _operations_snapshot(r32: dict[str, Any]) -> dict[str, Any]:
    r32_decision = r32.get("verification_decision") or {}
    r18 = r32.get("r18_scheduler_status") or {}
    parsed = r18.get("parsed_remote_state") or {}
    summaries = r32.get("ui_dashboard_truth_summaries") or {}
    dashboard = summaries.get("dashboard_snapshot_api") or {}
    writer = summaries.get("db_writer_api") or {}
    guard = summaries.get("workspace_guard_api") or {}
    paper_ready_raw = parsed.get("paper_ready_candidates")
    positive_ev_raw = parsed.get("positive_ev_rows")
    paper_ready = _int_value(paper_ready_raw)
    positive_ev = _int_value(positive_ev_raw)
    stale_watermarks = _int_value(dashboard.get("stale_watermark_count"))
    return {
        "private_base_url": r32_decision.get("private_base_url"),
        "r32_status": r32_decision.get("status"),
        "r32_verification_passed": bool(r32_decision.get("verification_passed")),
        "r18_status": r32_decision.get("r18_status"),
        "r5_pid": r32_decision.get("r5_pid"),
        "duplicate_r5": bool(r32_decision.get("duplicate_r5")),
        "service_owns_r5": bool(r32_decision.get("service_owns_r5")),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "watch_state": parsed.get("watch_state"),
        "paper_ready_candidates": paper_ready,
        "paper_ready_candidates_present": paper_ready_raw is not None,
        "positive_ev_rows": positive_ev,
        "positive_ev_rows_present": positive_ev_raw is not None,
        "writer_status": writer.get("writer_status"),
        "writer_safe_to_start_write": writer.get("safe_to_start_write"),
        "current_writer_pid": writer.get("current_writer_pid"),
        "workspace_guard_status": guard.get("summary_status"),
        "missing_required_commands": _int_value(guard.get("missing_required_commands")),
        "critical_findings": _int_value(guard.get("critical_findings")),
        "dashboard_snapshot_id": dashboard.get("dashboard_snapshot_id"),
        "dashboard_generated_at": dashboard.get("generated_at"),
        "dashboard_execution_mode": dashboard.get("effective_execution_mode"),
        "dashboard_required_watermark_count": _int_value(
            dashboard.get("required_watermark_count")
        ),
        "dashboard_stale_watermark_count": stale_watermarks,
        "paper_gate_state": (
            "PAPER_READY_REVIEW_REQUIRED" if paper_ready > 0 else "MONITORING_NO_TRADE"
        ),
    }


def _readiness_checks(r32: dict[str, Any], operations: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _check(
            "r32_dashboard_scheduler_verified",
            operations.get("r32_status") == VERIFIED_R32_STATUS
            and bool(operations.get("r32_verification_passed")),
            f"R32 status is {operations.get('r32_status')}.",
        ),
        _check(
            "private_ui_tailnet_ready",
            str(operations.get("private_base_url") or "").startswith("https://")
            and str(operations.get("private_base_url") or "").endswith(".ts.net"),
            f"Private base URL is {operations.get('private_base_url')}.",
        ),
        _check(
            "systemd_owns_single_r5",
            operations.get("r18_status") == "SYSTEMD_OWNS_R5"
            and bool(operations.get("service_owns_r5"))
            and not bool(operations.get("duplicate_r5")),
            (
                f"r18_status={operations.get('r18_status')}; "
                f"service_owns_r5={operations.get('service_owns_r5')}; "
                f"duplicate_r5={operations.get('duplicate_r5')}."
            ),
        ),
        _check(
            "r5_guard_running",
            operations.get("guard_status") == "RUNNING"
            and not bool(operations.get("guard_should_stop")),
            (
                f"guard_status={operations.get('guard_status')}; "
                f"guard_should_stop={operations.get('guard_should_stop')}."
            ),
        ),
        _check(
            "workspace_guard_clean",
            operations.get("workspace_guard_status") == "PASS"
            and _int_value(operations.get("missing_required_commands")) == 0
            and _int_value(operations.get("critical_findings")) == 0,
            (
                f"status={operations.get('workspace_guard_status')}; "
                f"missing={operations.get('missing_required_commands')}; "
                f"critical={operations.get('critical_findings')}."
            ),
        ),
        _check(
            "dashboard_truth_current",
            bool(operations.get("dashboard_snapshot_id"))
            and _int_value(operations.get("dashboard_required_watermark_count")) > 0,
            (
                f"snapshot={operations.get('dashboard_snapshot_id')}; "
                f"required_watermarks={operations.get('dashboard_required_watermark_count')}."
            ),
        ),
        _check(
            "dashboard_execution_mode_paper_shadow",
            operations.get("dashboard_execution_mode") in {None, PAPER_EXECUTION_MODE},
            f"dashboard_execution_mode={operations.get('dashboard_execution_mode')}.",
        ),
        _check(
            "paper_ready_count_explicit",
            bool(operations.get("paper_ready_candidates_present")),
            (
                f"paper_ready_candidates={operations.get('paper_ready_candidates')}; "
                f"present={operations.get('paper_ready_candidates_present')}."
            ),
        ),
        _check(
            "no_live_demo_or_paper_trade_side_effects",
            _all_false(
                r32.get("live_or_demo_execution"),
                r32.get("order_submission"),
                r32.get("order_cancel_replace"),
                r32.get("paper_trade_creation"),
            ),
            "R32 and R33 are read-only diagnostics.",
        ),
    ]


def _readiness_warnings(operations: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    stale_watermarks = _int_value(operations.get("dashboard_stale_watermark_count"))
    if stale_watermarks > 0:
        warnings.append(
            {
                "warning": "STALE_DASHBOARD_WATERMARKS",
                "severity": "warning",
                "detail": f"{stale_watermarks} dashboard source watermark(s) are stale.",
            }
        )
    if _int_value(operations.get("positive_ev_rows")) == 0:
        warnings.append(
            {
                "warning": "NO_CURRENT_POSITIVE_EV",
                "severity": "info",
                "detail": "R5 is running, but current positive_ev_rows is 0.",
            }
        )
    if _int_value(operations.get("paper_ready_candidates")) == 0:
        warnings.append(
            {
                "warning": "NO_PAPER_READY_CANDIDATES",
                "severity": "info",
                "detail": "Paper gate is closed; continue monitoring only.",
            }
        )
    return warnings


def _decision(
    checks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    operations: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    paper_ready = _int_value(operations.get("paper_ready_candidates"))
    if failed:
        status = "BLOCKED_PAPER_ONLY_OPERATIONS_READINESS"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R33 - Resolve Cloud Paper-Only Readiness"
        command = (
            "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness "
            "--output-dir reports/phase3bb_r33 --reports-dir reports"
        )
    elif paper_ready > 0:
        status = "PAPER_ONLY_OPERATOR_REVIEW_READY"
        reason = (
            "Cloud operations are paper-only ready and paper-ready candidates exist; "
            "use operator review, not automatic trade creation."
        )
        next_step = "Phase 3BB-R34 - Paper-Only Candidate Operator Review"
        command = "Open the private UI Opportunities page and inspect paper-only risk gates."
    else:
        status = "PAPER_ONLY_MONITORING_READY"
        reason = (
            "Cloud operations are safe for private paper-only monitoring; no paper-ready "
            "candidate exists right now."
        )
        next_step = "Phase 3BB-R34 - Cloud Multi-Category Refresh Scheduler Review"
        command = "Open the private UI System and Opportunities pages for monitoring."
    return {
        "status": status,
        "readiness_passed": not failed,
        "failed_check_count": len(failed),
        "warning_count": len(warnings),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "private_base_url": operations.get("private_base_url"),
        "r5_pid": operations.get("r5_pid"),
        "watch_state": operations.get("watch_state"),
        "positive_ev_rows": operations.get("positive_ev_rows"),
        "paper_ready_candidates": paper_ready,
        "paper_gate_state": operations.get("paper_gate_state"),
        "dashboard_stale_watermark_count": operations.get("dashboard_stale_watermark_count"),
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _all_false(*values: Any) -> bool:
    return all(not bool(value) for value in values)


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R33 Paper-Only Operations Readiness")
    decision = payload["readiness_decision"]
    operations = payload["operations_snapshot"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Readiness passed: `{decision['readiness_passed']}`",
            f"- Private base URL: `{decision['private_base_url']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Positive EV rows: `{decision['positive_ev_rows']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            f"- Paper gate state: `{decision['paper_gate_state']}`",
            f"- Dashboard stale watermarks: `{decision['dashboard_stale_watermark_count']}`",
            f"- Writer status: `{operations.get('writer_status')}`",
            f"- Workspace guard: `{operations.get('workspace_guard_status')}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- This command is diagnostic/read-only.",
            "- It does not create paper trades.",
            "- It does not submit, cancel, replace, or amend live/demo orders.",
            "- It does not start or stop UI, R5, Tailscale, nginx, or firewall services.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R33 Readiness Detail")
    decision = payload["readiness_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["readiness_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Warnings", ""])
    if not payload["readiness_warnings"]:
        lines.append("- None.")
    for row in payload["readiness_warnings"]:
        lines.append(f"- `{row['severity']}` `{row['warning']}` - {row['detail']}")
    lines.extend(["", "## Operations Snapshot", "", "```json"])
    lines.append(json.dumps(payload["operations_snapshot"], indent=2, sort_keys=True))
    lines.append("```")
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["readiness_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(decision['operator_next_command'])}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R33 Next Actions")
    decision = payload["readiness_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Command/action: `{decision['operator_next_command']}`",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly.",
            "- Do not enable Tailscale Funnel.",
            "- Do not stop or duplicate R5.",
            "- Do not create paper trades from this monitor.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
