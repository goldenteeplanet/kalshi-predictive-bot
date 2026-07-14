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

PHASE3BB_R16_VERSION = "phase3bb_r16_cloud_service_install_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r16")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R13_MAX_AGE_MINUTES = 30

FORBIDDEN_SERVICE_START_FRAGMENTS = (
    "systemctl restart",
    "systemctl start",
    "systemctl try-restart",
    "systemctl enable --now",
    "systemctl reenable",
)

FORBIDDEN_TRADING_FRAGMENTS = (
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
)


@dataclass(frozen=True)
class Phase3BBR16CloudServiceInstallHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    handoff_checks_path: Path
    operator_handoff_script_path: Path
    operator_next_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r16_cloud_service_install_handoff_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r13_max_age_minutes: int = DEFAULT_R13_MAX_AGE_MINUTES,
) -> Phase3BBR16CloudServiceInstallHandoffArtifacts:
    payload = build_phase3bb_r16_cloud_service_install_handoff(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        operator_approved=operator_approved,
        r13_max_age_minutes=r13_max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_service_install_handoff.md"
    json_path = output_dir / "cloud_service_install_handoff.json"
    handoff_checks_path = output_dir / "handoff_checks.csv"
    operator_handoff_script_path = output_dir / "operator_install_handoff.sh"
    operator_next_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_checks_csv(handoff_checks_path, payload["handoff_checks"])
    operator_handoff_script_path.write_text(_render_handoff_script(payload), encoding="utf-8")
    _mark_executable(operator_handoff_script_path)
    operator_next_command_path.write_text(_render_operator_next_command(payload), encoding="utf-8")
    _mark_executable(operator_next_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            handoff_checks_path,
            operator_handoff_script_path,
            operator_next_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR16CloudServiceInstallHandoffArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        handoff_checks_path=handoff_checks_path,
        operator_handoff_script_path=operator_handoff_script_path,
        operator_next_command_path=operator_next_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r16_cloud_service_install_handoff(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
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
        "command": "kalshi-bot phase3bb-r16-cloud-service-install-handoff",
        "argv": command_args or [],
    }
    r13_path = reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json"
    r14_path = reports_dir / "phase3bb_r14" / "cloud_service_plan.json"
    r15_path = reports_dir / "phase3bb_r15" / "cloud_service_install_review.json"
    r13 = _read_json(r13_path)
    r14 = _read_json(r14_path)
    r15 = _read_json(r15_path)
    service_plan = r14.get("service_plan") or {}
    service_name = str(service_plan.get("service_name") or "kalshi-r5-watcher.service")
    r14_dir = reports_dir / "phase3bb_r14"
    service_draft_path = r14_dir / f"{service_name}.draft"
    guard_script_draft_path = r14_dir / "kalshi-r5-start-guard.sh.draft"
    service_text = _read_text(service_draft_path)
    guard_text = _read_text(guard_script_draft_path)
    r13_age_seconds = _artifact_age_seconds(r13, now)
    target = _cloud_target(r13, r14)
    handoff_commands = _handoff_commands(
        target=target,
        service_name=service_name,
        guard_script_path=str(service_plan.get("guard_script_path") or ""),
    )
    handoff_checks = _handoff_checks(
        r13=r13,
        r14=r14,
        r15=r15,
        service_text=service_text,
        guard_text=guard_text,
        handoff_commands=handoff_commands,
        operator_approved=operator_approved,
        r13_age_seconds=r13_age_seconds,
        r13_max_age_minutes=r13_max_age_minutes,
    )
    decision = _handoff_decision(handoff_checks, r13, r14, r15, operator_approved)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_service_start": True,
        "no_live_or_demo_orders": True,
        "service_files_written_to_system": False,
        "operator_handoff_script_written": True,
        "operator_handoff_script_default_dry_run": True,
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
        "phase": "3BB-R16-CLOUD-SERVICE-INSTALL-HANDOFF",
        "phase_version": PHASE3BB_R16_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SERVICE_INSTALL_HANDOFF",
        "reports_dir": str(reports_dir),
        "r13_artifact_path": str(r13_path),
        "r14_artifact_path": str(r14_path),
        "r15_artifact_path": str(r15_path),
        "service_draft_path": str(service_draft_path),
        "guard_script_draft_path": str(guard_script_draft_path),
        "r13_context_available": bool(r13),
        "r14_context_available": bool(r14),
        "r15_context_available": bool(r15),
        "r13_age_seconds": r13_age_seconds,
        "r13_max_age_minutes": r13_max_age_minutes,
        "operator_approved": operator_approved,
        "cloud_target": target,
        "adoption_decision": r13.get("adoption_decision") or {},
        "service_plan": service_plan,
        "install_review_decision": r15.get("install_review_decision") or {},
        "handoff_decision": decision,
        "handoff_checks": handoff_checks,
        "handoff_commands": handoff_commands,
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


def _cloud_target(r13: dict[str, Any], r14: dict[str, Any]) -> dict[str, Any]:
    target = dict(r13.get("cloud_target") or r14.get("cloud_target") or {})
    service_plan = r14.get("service_plan") or {}
    return {
        "ssh_target": target.get("ssh_target") or "kalshi@159.65.35.72",
        "identity_file": target.get("identity_file") or "/home/james/.ssh/id_ed25519_do",
        "app_path": target.get("app_path") or service_plan.get("remote_app_path"),
        "env_path": target.get("env_path") or service_plan.get("remote_env_path"),
        "db_path": target.get("db_path") or service_plan.get("remote_db_path"),
        "reports_path": target.get("reports_path") or service_plan.get("remote_reports_path"),
    }


def _handoff_commands(
    *,
    target: dict[str, Any],
    service_name: str,
    guard_script_path: str,
) -> dict[str, str]:
    ssh_target = str(target["ssh_target"])
    identity_file = str(target["identity_file"])
    app_path = str(target["app_path"] or "/opt/kalshi-predictive-bot")
    guard_path = guard_script_path or f"{app_path}/scripts/cloud/kalshi-r5-start-guard.sh"
    service_tmp = f"/tmp/{service_name}"
    guard_tmp = "/tmp/kalshi-r5-start-guard.sh"
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    scp_prefix = f"scp -i {_shell_quote(identity_file)}"
    return {
        "refresh_r13": (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        ),
        "copy_service_draft": (
            f"{scp_prefix} reports/phase3bb_r14/{service_name}.draft "
            f"{_shell_quote(f'{ssh_target}:{service_tmp}')}"
        ),
        "copy_guard_draft": (
            f"{scp_prefix} reports/phase3bb_r14/kalshi-r5-start-guard.sh.draft "
            f"{_shell_quote(f'{ssh_target}:{guard_tmp}')}"
        ),
        "install_guard_script": (
            f"{ssh_prefix} 'sudo install -D -m 0755 {guard_tmp} {guard_path}'"
        ),
        "install_service_file": (
            f"{ssh_prefix} 'sudo install -m 0644 {service_tmp} "
            f"/etc/systemd/system/{service_name}'"
        ),
        "daemon_reload": f"{ssh_prefix} 'sudo systemctl daemon-reload'",
        "enable_no_start": f"{ssh_prefix} 'sudo systemctl enable {service_name}'",
        "verify_enabled": f"{ssh_prefix} 'systemctl is-enabled {service_name}'",
        "verify_not_running": f"{ssh_prefix} 'systemctl is-active {service_name} || true'",
        "verify_existing_r5": (
            f"{ssh_prefix} 'cd {app_path} && set -a && . "
            f"{target['env_path']} && set +a && .venv/bin/kalshi-bot "
            "phase3bc-r5-status --output-dir reports/phase3bc_r5'"
        ),
    }


def _handoff_checks(
    *,
    r13: dict[str, Any],
    r14: dict[str, Any],
    r15: dict[str, Any],
    service_text: str,
    guard_text: str,
    handoff_commands: dict[str, str],
    operator_approved: bool,
    r13_age_seconds: float | None,
    r13_max_age_minutes: int,
) -> list[dict[str, Any]]:
    r13_decision = r13.get("adoption_decision") or {}
    service_plan = r14.get("service_plan") or {}
    install_review = r15.get("install_review_decision") or {}
    all_commands = "\n".join(handoff_commands.values()).lower()
    draft_text = f"{service_text}\n{guard_text}".lower()
    forbidden_starts = [
        fragment for fragment in FORBIDDEN_SERVICE_START_FRAGMENTS if fragment in all_commands
    ]
    forbidden_trading = [
        fragment
        for fragment in FORBIDDEN_TRADING_FRAGMENTS
        if fragment in all_commands or fragment in draft_text
    ]
    max_age_seconds = max(1, r13_max_age_minutes) * 60
    return [
        _check("operator_approved_flag_present", operator_approved, "Operator approved R16."),
        _check("r13_artifact_present", bool(r13), "R13 adoption artifact exists."),
        _check(
            "r13_recently_refreshed",
            r13_age_seconds is not None and r13_age_seconds <= max_age_seconds,
            f"R13 artifact age is {r13_age_seconds} seconds.",
        ),
        _check(
            "r13_adopts_existing_r5",
            r13_decision.get("recommendation") == "ADOPT_EXISTING_R5",
            f"R13 recommendation is {r13_decision.get('recommendation')}.",
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
            "r13_no_duplicate_r5",
            r13_decision.get("duplicate_r5") is False,
            f"duplicate_r5={r13_decision.get('duplicate_r5')}.",
        ),
        _check(
            "r14_draft_ready",
            service_plan.get("status") == "DRAFT_READY_FOR_REVIEW",
            f"R14 status is {service_plan.get('status')}.",
        ),
        _check(
            "r15_install_review_ready",
            install_review.get("status") == "READY_FOR_OPERATOR_INSTALL_REVIEW_NO_START",
            f"R15 status is {install_review.get('status')}.",
        ),
        _check(
            "r15_no_failed_checks",
            install_review.get("failed_check_count") == 0,
            f"R15 failed checks: {install_review.get('failed_check_count')}.",
        ),
        _check(
            "service_and_guard_drafts_present",
            bool(service_text.strip()) and bool(guard_text.strip()),
            "R14 service and guard drafts are readable.",
        ),
        _check(
            "handoff_installs_enable_without_start",
            "systemctl enable" in all_commands and "systemctl start" not in all_commands,
            "Handoff contains enable-no-start commands only.",
        ),
        _check(
            "no_forbidden_service_start_commands",
            not forbidden_starts,
            f"Forbidden start hits: {', '.join(forbidden_starts) or 'none'}.",
        ),
        _check(
            "no_forbidden_trading_commands",
            not forbidden_trading,
            f"Forbidden trading hits: {', '.join(forbidden_trading) or 'none'}.",
        ),
    ]


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _handoff_decision(
    checks: list[dict[str, Any]],
    r13: dict[str, Any],
    r14: dict[str, Any],
    r15: dict[str, Any],
    operator_approved: bool,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r13_decision = r13.get("adoption_decision") or {}
    service_plan = r14.get("service_plan") or {}
    install_review = r15.get("install_review_decision") or {}
    if failed:
        status = "BLOCKED_INSTALL_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "HANDOFF_READY_ENABLE_NO_START"
        reason = (
            "Operator approval was present, R13 remains healthy, and R14/R15 are "
            "aligned. The handoff script defaults to dry-run and installs/enables "
            "only when the operator explicitly runs it with the approval token."
        )
    return {
        "status": status,
        "operator_approved": operator_approved,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "current_r5_pid": r13_decision.get("current_r5_pid"),
        "r13_recommendation": r13_decision.get("recommendation"),
        "r14_status": service_plan.get("status"),
        "r15_status": install_review.get("status"),
        "codex_executed_install": False,
        "codex_executed_enable": False,
        "codex_executed_start": False,
        "handoff_script_default_dry_run": True,
        "handoff_script_can_enable_no_start_with_token": not failed,
        "required_execute_token": "I_APPROVE_R16_INSTALL",
        "operator_next_command": (
            "bash reports/phase3bb_r16/operator_install_handoff.sh"
        ),
        "next_codex_step": (
            "Phase 3BB-R17 - Cloud Service Install Verification After Operator Run"
        ),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R16 Cloud Service Install Handoff")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- Operator approved flag: `{decision['operator_approved']}`",
            f"- Current R5 PID: `{decision['current_r5_pid']}`",
            f"- R13 recommendation: `{decision['r13_recommendation']}`",
            f"- R14 status: `{decision['r14_status']}`",
            f"- R15 status: `{decision['r15_status']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## What This Phase Did",
            "",
            "- Wrote an operator handoff script.",
            "- Kept that script dry-run by default.",
            "- Included install + enable-no-start commands only for explicit operator use.",
            "",
            "## Safety",
            "",
            "- Codex did not copy service files to the cloud.",
            "- Codex did not install, enable, or start the service.",
            "- Existing R5 was not stopped.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Operator Command",
            "",
            f"```bash\n{decision['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R16 Install Handoff Detail")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Handoff",
            "",
            "The generated handoff script installs the reviewed R14 service and guard "
            "drafts, reloads systemd, and enables the service without starting it. "
            "This avoids duplicate R5 watchers while preparing systemd to own the "
            "next clean startup.",
            "",
            f"- Decision: `{decision['status']}`",
            f"- Execute token: `{decision['required_execute_token']}`",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["handoff_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Handoff Commands", ""])
    for name, command in payload["handoff_commands"].items():
        lines.extend([f"### {name}", "", "```bash", command, "```", ""])
    return "\n".join(lines) + "\n"


def _render_handoff_script(payload: dict[str, Any]) -> str:
    commands = payload["handoff_commands"]
    decision = payload["handoff_decision"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "TOKEN=${PHASE3BB_R16_EXECUTE:-}",
        f"REQUIRED={_shell_quote(decision['required_execute_token'])}",
        "",
        "echo '[phase3bb-r16] cloud service install handoff'",
        "echo '[phase3bb-r16] default mode is dry-run; no remote changes occur'",
        "",
        "commands=(",
    ]
    for command in commands.values():
        lines.append(f"  {_shell_quote(command)}")
    lines.extend(
        [
            ")",
            "",
            "if [[ \"$TOKEN\" != \"$REQUIRED\" ]]; then",
            "  echo '[phase3bb-r16] dry-run command list:'",
            "  printf '  %s\\n' \"${commands[@]}\"",
            "  echo '[phase3bb-r16] no install/enable/start command executed'",
            "  echo '[phase3bb-r16] to execute install+enable-no-start, set:'",
            "  echo \"  PHASE3BB_R16_EXECUTE=$REQUIRED bash $0\"",
            "  exit 0",
            "fi",
            "",
            "echo '[phase3bb-r16] approval token accepted'",
            "echo '[phase3bb-r16] running install + enable-no-start handoff'",
            "for command in \"${commands[@]}\"; do",
            "  echo \"+ $command\"",
            "  bash -lc \"$command\"",
            "done",
            "echo '[phase3bb-r16] handoff commands completed'",
            "echo '[phase3bb-r16] verify with Phase 3BB-R17 next'",
            "",
        ]
    )
    return "\n".join(lines)


def _render_operator_next_command(payload: dict[str, Any]) -> str:
    decision = payload["handoff_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Default dry-run. This prints the operator handoff commands only.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R16 Next Actions")
    decision = payload["handoff_decision"]
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
            "This prints the command list only. To execute the install+enable-no-start "
            "handoff, rerun it with `PHASE3BB_R16_EXECUTE=I_APPROVE_R16_INSTALL`.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not run `systemctl start` for the service while PID 1917 is active.",
            "- Do not stop the existing R5 watcher.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
