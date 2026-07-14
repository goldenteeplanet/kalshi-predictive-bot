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
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BB_R15_VERSION = "phase3bb_r15_cloud_service_install_review_no_start_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r15")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R13_MAX_AGE_MINUTES = 30

FORBIDDEN_DRAFT_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "autopilot-run",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "place-order",
    "replace-order",
    "submit-order",
    "systemctl enable",
    "systemctl restart",
    "systemctl start",
)


@dataclass(frozen=True)
class Phase3BBR15CloudServiceInstallReviewArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    review_csv_path: Path
    no_start_dry_run_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r15_cloud_service_install_review_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r13_max_age_minutes: int = DEFAULT_R13_MAX_AGE_MINUTES,
) -> Phase3BBR15CloudServiceInstallReviewArtifacts:
    payload = build_phase3bb_r15_cloud_service_install_review(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        r13_max_age_minutes=r13_max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_service_install_review.md"
    json_path = output_dir / "cloud_service_install_review.json"
    review_csv_path = output_dir / "service_review_checks.csv"
    no_start_dry_run_path = output_dir / "install_review_no_start_dry_run.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_review_csv(review_csv_path, payload["review_checks"])
    no_start_dry_run_path.write_text(_render_no_start_dry_run(payload), encoding="utf-8")
    _mark_executable(no_start_dry_run_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            review_csv_path,
            no_start_dry_run_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR15CloudServiceInstallReviewArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        review_csv_path=review_csv_path,
        no_start_dry_run_path=no_start_dry_run_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r15_cloud_service_install_review(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r13_max_age_minutes: int = DEFAULT_R13_MAX_AGE_MINUTES,
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
        "command": "kalshi-bot phase3bb-r15-cloud-service-install-review",
        "argv": command_args or [],
    }
    r13_path = reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json"
    r14_path = reports_dir / "phase3bb_r14" / "cloud_service_plan.json"
    r13 = _read_json(r13_path)
    r14 = _read_json(r14_path)
    service_plan = r14.get("service_plan") or {}
    service_name = str(service_plan.get("service_name") or "kalshi-r5-watcher.service")
    r14_dir = reports_dir / "phase3bb_r14"
    service_draft_path = r14_dir / f"{service_name}.draft"
    guard_script_draft_path = r14_dir / "kalshi-r5-start-guard.sh.draft"
    checklist_path = r14_dir / "install_review_checklist.md"
    service_text = _read_text(service_draft_path)
    guard_text = _read_text(guard_script_draft_path)
    checklist_text = _read_text(checklist_path)
    r13_age_seconds = _artifact_age_seconds(r13, now)
    review_checks = _review_checks(
        r13=r13,
        r14=r14,
        service_text=service_text,
        guard_text=guard_text,
        checklist_text=checklist_text,
        r13_age_seconds=r13_age_seconds,
        r13_max_age_minutes=r13_max_age_minutes,
    )
    decision = _install_review_decision(review_checks, r13, r14)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
        "no_service_start": True,
        "no_service_enable": True,
        "service_files_written_to_system": False,
        "systemctl_commands_executed": 0,
        "ssh_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R15-CLOUD-SERVICE-INSTALL-REVIEW-NO-START",
        "phase_version": PHASE3BB_R15_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SERVICE_INSTALL_REVIEW_NO_START",
        "reports_dir": str(reports_dir),
        "r13_artifact_path": str(r13_path),
        "r14_artifact_path": str(r14_path),
        "service_draft_path": str(service_draft_path),
        "guard_script_draft_path": str(guard_script_draft_path),
        "install_review_checklist_path": str(checklist_path),
        "r13_context_available": bool(r13),
        "r14_context_available": bool(r14),
        "r13_age_seconds": r13_age_seconds,
        "r13_max_age_minutes": r13_max_age_minutes,
        "adoption_decision": r13.get("adoption_decision") or {},
        "service_plan": service_plan,
        "install_review_decision": decision,
        "review_checks": review_checks,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _artifact_age_seconds(payload: dict[str, Any], now: Any) -> float | None:
    parsed = parse_datetime(payload.get("generated_at"))
    if parsed is None:
        return None
    return max(0.0, round((now - parsed).total_seconds(), 3))


def _review_checks(
    *,
    r13: dict[str, Any],
    r14: dict[str, Any],
    service_text: str,
    guard_text: str,
    checklist_text: str,
    r13_age_seconds: float | None,
    r13_max_age_minutes: int,
) -> list[dict[str, Any]]:
    r13_decision = r13.get("adoption_decision") or {}
    service_plan = r14.get("service_plan") or {}
    r13_pid = r13_decision.get("current_r5_pid")
    r14_pid = service_plan.get("existing_r5_pid")
    r13_recommendation = r13_decision.get("recommendation")
    r14_recommendation = service_plan.get("r13_recommendation")
    max_age_seconds = max(1, r13_max_age_minutes) * 60
    forbidden_hits = [
        fragment
        for fragment in FORBIDDEN_DRAFT_FRAGMENTS
        if fragment in service_text.lower() or fragment in guard_text.lower()
    ]
    checks = [
        _check("r13_artifact_present", bool(r13), "R13 adoption artifact exists."),
        _check(
            "r13_recently_refreshed",
            r13_age_seconds is not None and r13_age_seconds <= max_age_seconds,
            f"R13 artifact age is {r13_age_seconds} seconds.",
        ),
        _check(
            "r13_adopts_existing_r5",
            r13_recommendation == "ADOPT_EXISTING_R5",
            f"R13 recommendation is {r13_recommendation}.",
        ),
        _check(
            "r13_no_duplicate_r5",
            r13_decision.get("duplicate_r5") is False,
            f"duplicate_r5={r13_decision.get('duplicate_r5')}.",
        ),
        _check(
            "r13_guard_healthy",
            r13_decision.get("guard_status") == "RUNNING"
            and r13_decision.get("guard_should_stop") is False,
            (
                f"guard_status={r13_decision.get('guard_status')}, "
                f"guard_should_stop={r13_decision.get('guard_should_stop')}."
            ),
        ),
        _check(
            "r14_draft_ready",
            service_plan.get("status") == "DRAFT_READY_FOR_REVIEW",
            f"R14 service plan status is {service_plan.get('status')}.",
        ),
        _check(
            "r14_matches_current_r13_pid",
            r13_pid is not None and r13_pid == r14_pid,
            f"R13 PID={r13_pid}; R14 PID={r14_pid}.",
        ),
        _check(
            "r14_matches_current_r13_recommendation",
            r13_recommendation == r14_recommendation == "ADOPT_EXISTING_R5",
            f"R13={r13_recommendation}; R14={r14_recommendation}.",
        ),
        _check(
            "service_draft_present",
            bool(service_text.strip()),
            "R14 service draft file is readable.",
        ),
        _check(
            "service_uses_guard",
            "ExecStartPre=" in service_text and "kalshi-r5-start-guard.sh" in service_text,
            "Service draft has an ExecStartPre guard.",
        ),
        _check(
            "service_runs_r5_foreground",
            "phase3bc-r5-crypto-freshness-watch" in service_text,
            "Service draft ExecStart runs the R5 freshness watcher.",
        ),
        _check(
            "service_has_paper_only_identity",
            "User=kalshi" in service_text and "EnvironmentFile=" in service_text,
            "Service draft pins user and environment file.",
        ),
        _check(
            "guard_script_present",
            bool(guard_text.strip()),
            "R14 guard script draft is readable.",
        ),
        _check(
            "guard_blocks_duplicate_r5",
            "pgrep -f 'phase3bc-r5-crypto-freshness-watch'" in guard_text
            and "Refusing duplicate R5 start" in guard_text,
            "Guard script refuses duplicate R5 watcher starts.",
        ),
        _check(
            "guard_checks_writer",
            "db-writer-monitor --json" in guard_text and "safe_to_start_write" in guard_text,
            "Guard script checks db-writer-monitor before start.",
        ),
        _check(
            "install_checklist_present",
            "R14 Is Draft Only" in checklist_text,
            "R14 install review checklist is present.",
        ),
        _check(
            "no_forbidden_fragments_in_drafts",
            not forbidden_hits,
            f"Forbidden hits: {', '.join(forbidden_hits) if forbidden_hits else 'none'}.",
        ),
    ]
    return checks


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _install_review_decision(
    checks: list[dict[str, Any]],
    r13: dict[str, Any],
    r14: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r13_decision = r13.get("adoption_decision") or {}
    service_plan = r14.get("service_plan") or {}
    if failed:
        status = "BLOCKED_INSTALL_REVIEW"
        reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "READY_FOR_OPERATOR_INSTALL_REVIEW_NO_START"
        reason = (
            "R13 was refreshed and still recommends ADOPT_EXISTING_R5; R14 draft "
            "matches the healthy watcher. This phase still forbids install/start."
        )
    return {
        "status": status,
        "ready_for_operator_review": not failed,
        "install_allowed_now": False,
        "start_allowed_now": False,
        "enable_allowed_now": False,
        "copy_to_remote_allowed_now": False,
        "requires_explicit_operator_approval": True,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "current_r5_pid": r13_decision.get("current_r5_pid"),
        "r13_recommendation": r13_decision.get("recommendation"),
        "r14_status": service_plan.get("status"),
        "operator_next_command": (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        ),
        "next_codex_step": (
            "Phase 3BB-R16 - Operator-Approved Cloud Service Install Handoff"
        ),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R15 Cloud Service Install Review")
    decision = payload["install_review_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Ready for operator review: `{decision['ready_for_operator_review']}`",
            f"- Install allowed now: `{decision['install_allowed_now']}`",
            f"- Start allowed now: `{decision['start_allowed_now']}`",
            f"- Enable allowed now: `{decision['enable_allowed_now']}`",
            f"- Copy to remote allowed now: `{decision['copy_to_remote_allowed_now']}`",
            f"- Current R5 PID: `{decision['current_r5_pid']}`",
            f"- R13 recommendation: `{decision['r13_recommendation']}`",
            f"- R14 status: `{decision['r14_status']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- No service was installed.",
            "- No service was enabled or started.",
            "- Existing R5 was not stopped.",
            "- No SSH or systemctl command was executed.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Operator Check",
            "",
            f"```bash\n{decision['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R15 Install Review Detail")
    decision = payload["install_review_decision"]
    lines.extend(
        [
            "",
            "## Review Scope",
            "",
            "This phase reviews the refreshed R13 adoption gate and the R14 service "
            "draft. It does not copy service files, install systemd units, start "
            "services, stop R5, or create trades.",
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["review_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(
        [
            "",
            "## Draft Paths",
            "",
            f"- Service draft: `{payload['service_draft_path']}`",
            f"- Guard script draft: `{payload['guard_script_draft_path']}`",
            f"- Install checklist: `{payload['install_review_checklist_path']}`",
            "",
            "## No-Start Rule",
            "",
            "The generated dry-run script performs local artifact checks only. It does "
            "not contain `systemctl start`, `systemctl enable`, `scp`, or service "
            "installation commands.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_no_start_dry_run(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r15] no-start install review dry run'",
            "echo '[phase3bb-r15] checking local draft artifacts only'",
            "test -f reports/phase3bb_r13/cloud_scheduler_adoption.json",
            "test -f reports/phase3bb_r14/cloud_service_plan.json",
            "test -f reports/phase3bb_r14/kalshi-r5-watcher.service.draft",
            "test -f reports/phase3bb_r14/kalshi-r5-start-guard.sh.draft",
            "grep -q 'ExecStartPre=.*kalshi-r5-start-guard.sh' "
            "reports/phase3bb_r14/kalshi-r5-watcher.service.draft",
            "grep -q 'phase3bc-r5-crypto-freshness-watch' "
            "reports/phase3bb_r14/kalshi-r5-watcher.service.draft",
            "grep -q 'Refusing duplicate R5 start' "
            "reports/phase3bb_r14/kalshi-r5-start-guard.sh.draft",
            "grep -q 'db-writer-monitor --json' "
            "reports/phase3bb_r14/kalshi-r5-start-guard.sh.draft",
            "echo '[phase3bb-r15] dry run checks passed'",
            "echo '[phase3bb-r15] no install/start/enable command executed'",
            "",
        ]
    )


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["install_review_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# R15 is no-start. Re-run R13 before any later install/start phase.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R15 Next Actions")
    decision = payload["install_review_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run Yet",
            "",
            "- Do not install the service draft.",
            "- Do not copy the service draft to `/etc/systemd/system`.",
            "- Do not enable or start the service.",
            "- Do not stop the existing R5 watcher.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
