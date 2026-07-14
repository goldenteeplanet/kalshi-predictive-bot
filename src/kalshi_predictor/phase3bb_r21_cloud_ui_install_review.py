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

PHASE3BB_R21_VERSION = "phase3bb_r21_cloud_ui_install_review_no_start_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r21")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R20_MAX_AGE_MINUTES = 30
DEFAULT_UI_SERVICE_NAME = "kalshi-ui.service"

FORBIDDEN_UI_DRAFT_FRAGMENTS = (
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
    "ufw allow",
)


@dataclass(frozen=True)
class Phase3BBR21CloudUiInstallReviewArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    review_csv_path: Path
    no_start_dry_run_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r21_cloud_ui_install_review_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r20_max_age_minutes: int = DEFAULT_R20_MAX_AGE_MINUTES,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
) -> Phase3BBR21CloudUiInstallReviewArtifacts:
    payload = build_phase3bb_r21_cloud_ui_install_review(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        r20_max_age_minutes=r20_max_age_minutes,
        ui_service_name=ui_service_name,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_install_review.md"
    json_path = output_dir / "cloud_ui_install_review.json"
    review_csv_path = output_dir / "ui_install_review_checks.csv"
    no_start_dry_run_path = output_dir / "ui_install_review_no_start_dry_run.sh"
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
    return Phase3BBR21CloudUiInstallReviewArtifacts(
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


def build_phase3bb_r21_cloud_ui_install_review(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    r20_max_age_minutes: int = DEFAULT_R20_MAX_AGE_MINUTES,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
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
        "command": "kalshi-bot phase3bb-r21-cloud-ui-install-review",
        "argv": command_args or [],
    }
    r20_path = reports_dir / "phase3bb_r20" / "cloud_ui_service_plan.json"
    r20 = _read_json(r20_path)
    r20_dir = reports_dir / "phase3bb_r20"
    service_draft_path = r20_dir / f"{ui_service_name}.draft"
    nginx_draft_path = r20_dir / "kalshi-ui.nginx.draft"
    checklist_path = r20_dir / "ui_install_review_checklist.md"
    service_text = _read_text(service_draft_path)
    nginx_text = _read_text(nginx_draft_path)
    checklist_text = _read_text(checklist_path)
    r20_age_seconds = _artifact_age_seconds(r20, now)
    review_checks = _review_checks(
        r20=r20,
        service_text=service_text,
        nginx_text=nginx_text,
        checklist_text=checklist_text,
        r20_age_seconds=r20_age_seconds,
        r20_max_age_minutes=r20_max_age_minutes,
    )
    decision = _install_review_decision(review_checks, r20)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
        "no_service_start": True,
        "no_service_enable": True,
        "no_nginx_install": True,
        "no_firewall_change": True,
        "service_files_written_to_system": False,
        "systemctl_commands_executed": 0,
        "ssh_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_ui_service": False,
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
        "phase": "3BB-R21-CLOUD-UI-INSTALL-REVIEW-NO-START",
        "phase_version": PHASE3BB_R21_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_INSTALL_REVIEW_NO_START",
        "reports_dir": str(reports_dir),
        "r20_artifact_path": str(r20_path),
        "service_draft_path": str(service_draft_path),
        "nginx_draft_path": str(nginx_draft_path),
        "install_review_checklist_path": str(checklist_path),
        "r20_context_available": bool(r20),
        "r20_age_seconds": r20_age_seconds,
        "r20_max_age_minutes": r20_max_age_minutes,
        "ui_service_plan": r20.get("ui_service_plan") or {},
        "parsed_ui_state": r20.get("parsed_ui_state") or {},
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
    r20: dict[str, Any],
    service_text: str,
    nginx_text: str,
    checklist_text: str,
    r20_age_seconds: float | None,
    r20_max_age_minutes: int,
) -> list[dict[str, Any]]:
    plan = r20.get("ui_service_plan") or {}
    ui_state = r20.get("parsed_ui_state") or {}
    max_age_seconds = max(1, r20_max_age_minutes) * 60
    combined_text = "\n".join([service_text.lower(), nginx_text.lower()])
    forbidden_hits = [
        fragment for fragment in FORBIDDEN_UI_DRAFT_FRAGMENTS if fragment in combined_text
    ]
    checks = [
        _check("r20_artifact_present", bool(r20), "R20 UI service plan artifact exists."),
        _check(
            "r20_recently_refreshed",
            r20_age_seconds is not None and r20_age_seconds <= max_age_seconds,
            f"R20 artifact age is {r20_age_seconds} seconds.",
        ),
        _check(
            "r20_draft_ready",
            plan.get("status") == "DRAFT_READY_FOR_REVIEW",
            f"R20 status is {plan.get('status')}.",
        ),
        _check(
            "r20_r5_systemd_owned",
            plan.get("r18_status") == "SYSTEMD_OWNS_R5",
            f"R18 status in R20 is {plan.get('r18_status')}.",
        ),
        _check(
            "r20_no_duplicate_ui",
            ui_state.get("ui_duplicate_process") is False,
            f"ui_duplicate_process={ui_state.get('ui_duplicate_process')}.",
        ),
        _check(
            "r20_public_exposure_deferred",
            plan.get("expose_public_allowed_now") is False,
            f"expose_public_allowed_now={plan.get('expose_public_allowed_now')}.",
        ),
        _check(
            "service_draft_present",
            bool(service_text.strip()),
            "R20 UI service draft is readable.",
        ),
        _check(
            "service_is_localhost_only",
            "--host 127.0.0.1" in service_text and "--host 0.0.0.0" not in service_text,
            "UI service binds to 127.0.0.1 only.",
        ),
        _check(
            "service_uses_expected_port",
            "--port 8080" in service_text,
            "UI service binds to port 8080.",
        ),
        _check(
            "service_runs_ui_command",
            "kalshi-bot ui" in service_text,
            "Service draft runs the registered UI command.",
        ),
        _check(
            "service_has_readonly_flags",
            "Environment=UI_READ_ONLY=true" in service_text
            and "Environment=EXECUTION_ENABLED=false" in service_text
            and "Environment=EXECUTION_DRY_RUN=true" in service_text
            and "Environment=EXECUTION_KILL_SWITCH=true" in service_text,
            "Service draft pins read-only execution flags.",
        ),
        _check(
            "service_depends_on_r5",
            "Requires=kalshi-r5-watcher.service" in service_text
            and "After=network-online.target kalshi-r5-watcher.service" in service_text,
            "UI service starts after the R5 service.",
        ),
        _check(
            "service_has_safe_identity",
            "User=kalshi" in service_text and "EnvironmentFile=" in service_text,
            "Service draft pins user and environment file.",
        ),
        _check(
            "service_has_limited_write_paths",
            "ReadWritePaths=" in service_text and "ProtectSystem=full" in service_text,
            "Service draft constrains write paths.",
        ),
        _check(
            "nginx_draft_present",
            bool(nginx_text.strip()),
            "R20 nginx draft is readable.",
        ),
        _check(
            "nginx_draft_marked_deferred",
            "Draft only" in nginx_text and "Do not install" in nginx_text,
            "Nginx draft is explicitly marked deferred.",
        ),
        _check(
            "nginx_proxy_is_localhost_only",
            "proxy_pass http://127.0.0.1:8080;" in nginx_text,
            "Nginx draft proxies only to localhost UI.",
        ),
        _check(
            "install_checklist_present",
            "R20 Is Draft Only" in checklist_text,
            "R20 UI install review checklist is present.",
        ),
        _check(
            "no_forbidden_fragments_in_drafts",
            not forbidden_hits,
            f"Forbidden hits: {', '.join(forbidden_hits) if forbidden_hits else 'none'}.",
        ),
    ]
    return checks


def _install_review_decision(
    checks: list[dict[str, Any]],
    r20: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    plan = r20.get("ui_service_plan") or {}
    if failed:
        status = "BLOCKED_UI_INSTALL_REVIEW"
        reason = f"First failing check: {failed[0]['check']}."
        if _r5_runtime_blocked(plan):
            operator_command = (
                "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
                "--output-dir reports/phase3bb_r18 --reports-dir reports"
            )
            next_step = "Phase 3BB-R18 - Resolve R5 Runtime Blocker Before UI Install"
        else:
            operator_command = (
                "kalshi-bot phase3bb-r20-cloud-ui-service-plan "
                "--output-dir reports/phase3bb_r20 --reports-dir reports"
            )
            next_step = "Phase 3BB-R20 - Refresh Cloud UI Service Plan"
    else:
        status = "READY_FOR_OPERATOR_UI_INSTALL_REVIEW_NO_START"
        reason = (
            "R20 is fresh, the UI service draft is localhost-only and read-only, "
            "and public exposure remains deferred. This phase still forbids install/start."
        )
        operator_command = (
            "kalshi-bot phase3bb-r20-cloud-ui-service-plan "
            "--output-dir reports/phase3bb_r20 --reports-dir reports"
        )
        next_step = "Phase 3BB-R22 - Operator-Approved Cloud UI Install Handoff"
    return {
        "status": status,
        "ready_for_operator_review": not failed,
        "install_allowed_now": False,
        "start_allowed_now": False,
        "enable_allowed_now": False,
        "copy_to_remote_allowed_now": False,
        "public_exposure_allowed_now": False,
        "requires_explicit_operator_approval": True,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r20_status": plan.get("status"),
        "r18_status": plan.get("r18_status"),
        "r5_pid": plan.get("r5_pid"),
        "ui_service_name": plan.get("ui_service_name"),
        "ssh_tunnel_command": plan.get("ssh_tunnel_command"),
        "operator_next_command": operator_command,
        "next_codex_step": next_step,
    }


def _r5_runtime_blocked(plan: dict[str, Any]) -> bool:
    return plan.get("r18_status") != "SYSTEMD_OWNS_R5"


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R21 Cloud UI Install Review")
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
            f"- Public exposure allowed now: `{decision['public_exposure_allowed_now']}`",
            f"- R20 status: `{decision['r20_status']}`",
            f"- R18 status: `{decision['r18_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- No UI service was installed, enabled, or started.",
            "- No nginx or firewall change was made.",
            "- No SSH, scp, or systemctl command was executed.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Operator Check",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R21 UI Install Review Detail")
    decision = payload["install_review_decision"]
    lines.extend(
        [
            "",
            "## Review Scope",
            "",
            "This phase reviews the R20 UI service draft and nginx draft. It does not "
            "copy files, install systemd units, enable/start services, alter nginx, "
            "open firewall ports, or create trades.",
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
            f"- Nginx draft: `{payload['nginx_draft_path']}`",
            f"- Install checklist: `{payload['install_review_checklist_path']}`",
            "",
            "## No-Start Rule",
            "",
            "The dry-run script checks local artifacts only. It does not contain "
            "`systemctl start`, `systemctl enable`, `scp`, `ufw`, nginx install, "
            "or service installation commands.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_no_start_dry_run(payload: dict[str, Any]) -> str:
    service_path = payload["service_draft_path"]
    nginx_path = payload["nginx_draft_path"]
    r20_path = payload["r20_artifact_path"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r21] no-start UI install review dry run'",
            "echo '[phase3bb-r21] checking local draft artifacts only'",
            f"test -f {service_path}",
            f"test -f {nginx_path}",
            f"test -f {r20_path}",
            f"grep -q 'kalshi-bot ui --host 127.0.0.1 --port 8080' {service_path}",
            f"grep -q 'Environment=UI_READ_ONLY=true' {service_path}",
            f"grep -q 'Environment=EXECUTION_ENABLED=false' {service_path}",
            f"grep -q 'Environment=EXECUTION_DRY_RUN=true' {service_path}",
            f"grep -q 'Environment=EXECUTION_KILL_SWITCH=true' {service_path}",
            f"grep -q 'Requires=kalshi-r5-watcher.service' {service_path}",
            f"grep -q 'proxy_pass http://127.0.0.1:8080;' {nginx_path}",
            f"grep -q 'Draft only' {nginx_path}",
            "echo '[phase3bb-r21] dry run checks passed'",
            "echo '[phase3bb-r21] no install/start/enable/nginx/firewall command executed'",
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
            "# R21 is no-start. Re-run R20 before any later UI install/start phase.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R21 Next Actions")
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
            "- Do not install the UI service draft.",
            "- Do not copy the UI service draft to `/etc/systemd/system`.",
            "- Do not enable or start the UI service.",
            "- Do not install nginx or open firewall ports.",
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
