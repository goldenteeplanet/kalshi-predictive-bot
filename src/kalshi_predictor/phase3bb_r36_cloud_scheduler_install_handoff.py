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

PHASE3BB_R36_VERSION = "phase3bb_r36_cloud_scheduler_install_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r36")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R35_MAX_AGE_MINUTES = 60
APPROVAL_ENV_VAR = "PHASE3BB_R36_EXECUTE"
APPROVAL_TOKEN = "I_APPROVE_R36_SCHEDULER_INSTALL"
SCHEDULER_SERVICE_NAME = "kalshi-multicategory-refresh-scheduler.service"
SCHEDULER_TIMER_NAME = "kalshi-multicategory-refresh-scheduler.timer"
RUNNER_SCRIPT_NAME = "kalshi-multicategory-refresh-runner.sh"
READY_R35_STATUS = "READY_FOR_OPERATOR_APPROVED_SCHEDULER_INSTALL_HANDOFF"

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
class Phase3BBR36CloudSchedulerInstallHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    handoff_checks_path: Path
    operator_handoff_script_path: Path
    operator_next_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r36_cloud_scheduler_install_handoff_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r35_max_age_minutes: int = DEFAULT_R35_MAX_AGE_MINUTES,
) -> Phase3BBR36CloudSchedulerInstallHandoffArtifacts:
    payload = build_phase3bb_r36_cloud_scheduler_install_handoff(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        operator_approved=operator_approved,
        r35_max_age_minutes=r35_max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_install_handoff.md"
    json_path = output_dir / "cloud_scheduler_install_handoff.json"
    handoff_checks_path = output_dir / "handoff_checks.csv"
    operator_handoff_script_path = output_dir / "operator_scheduler_install_handoff.sh"
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
    return Phase3BBR36CloudSchedulerInstallHandoffArtifacts(
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


def build_phase3bb_r36_cloud_scheduler_install_handoff(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r35_max_age_minutes: int = DEFAULT_R35_MAX_AGE_MINUTES,
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
        "command": "kalshi-bot phase3bb-r36-cloud-scheduler-install-handoff",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r35_path = reports_dir / "phase3bb_r35" / "cloud_multicategory_scheduler_no_start_dry_run.json"
    r35_dir = reports_dir / "phase3bb_r35"
    service_draft_path = r35_dir / f"{SCHEDULER_SERVICE_NAME}.draft"
    timer_draft_path = r35_dir / f"{SCHEDULER_TIMER_NAME}.draft"
    runner_draft_path = r35_dir / f"{RUNNER_SCRIPT_NAME}.draft"
    r11 = _read_json(r11_path)
    r35 = _read_json(r35_path)
    r35_age_seconds = _artifact_age_seconds(r35, now)
    service_text = _read_text(service_draft_path)
    timer_text = _read_text(timer_draft_path)
    runner_text = _read_text(runner_draft_path)
    target = _cloud_target(r11)
    handoff_commands = _handoff_commands(target=target)
    handoff_checks = _handoff_checks(
        r11=r11,
        r35=r35,
        service_text=service_text,
        timer_text=timer_text,
        runner_text=runner_text,
        handoff_commands=handoff_commands,
        operator_approved=operator_approved,
        r35_age_seconds=r35_age_seconds,
        r35_max_age_minutes=r35_max_age_minutes,
    )
    decision = _handoff_decision(handoff_checks, r35, operator_approved)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "operator_handoff_script_written": True,
        "operator_handoff_script_default_dry_run": True,
        "no_service_start": True,
        "service_files_written_to_system": False,
        "scheduler_files_written_to_system": False,
        "systemctl_commands_executed": 0,
        "ssh_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_scheduler": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R36-CLOUD-SCHEDULER-INSTALL-HANDOFF",
        "phase_version": PHASE3BB_R36_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_INSTALL_HANDOFF",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r35_artifact_path": str(r35_path),
        "service_draft_path": str(service_draft_path),
        "timer_draft_path": str(timer_draft_path),
        "runner_draft_path": str(runner_draft_path),
        "r11_context_available": bool(r11),
        "r35_context_available": bool(r35),
        "r35_age_seconds": r35_age_seconds,
        "r35_max_age_minutes": r35_max_age_minutes,
        "operator_approved": operator_approved,
        "cloud_target": target,
        "dry_run_decision": r35.get("dry_run_decision") or {},
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


def _cloud_target(r11: dict[str, Any]) -> dict[str, Any]:
    ssh_profile = r11.get("ssh_profile") or {}
    remote_paths = r11.get("remote_paths") or {}
    return {
        "ssh_target": _ssh_target(ssh_profile),
        "identity_file": ssh_profile.get("identity_file") or "/home/james/.ssh/id_ed25519_do",
        "app_path": remote_paths.get("app_path") or "/opt/kalshi-predictive-bot",
        "env_path": remote_paths.get("env_path") or "/etc/kalshi-bot/kalshi-bot.env",
        "db_path": remote_paths.get("db_path") or "/var/lib/kalshi-bot/kalshi_phase1.db",
        "reports_path": remote_paths.get("reports_path") or "/opt/kalshi-predictive-bot/reports",
    }


def _ssh_target(ssh_profile: dict[str, Any]) -> str:
    explicit = ssh_profile.get("ssh_target")
    if explicit:
        return str(explicit)
    user = ssh_profile.get("user") or "kalshi"
    host = ssh_profile.get("host") or "159.65.35.72"
    return f"{user}@{host}"


def _handoff_commands(*, target: dict[str, Any]) -> dict[str, str]:
    ssh_target = str(target["ssh_target"])
    identity_file = str(target["identity_file"])
    app_path = str(target["app_path"] or "/opt/kalshi-predictive-bot")
    env_path = str(target["env_path"] or "/etc/kalshi-bot/kalshi-bot.env")
    runner_remote = f"{app_path}/scripts/{RUNNER_SCRIPT_NAME}"
    service_tmp = f"/tmp/{SCHEDULER_SERVICE_NAME}"
    timer_tmp = f"/tmp/{SCHEDULER_TIMER_NAME}"
    runner_tmp = f"/tmp/{RUNNER_SCRIPT_NAME}"
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    scp_prefix = f"scp -i {_shell_quote(identity_file)}"
    return {
        "refresh_r35": (
            ".venv/bin/kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-"
            "dry-run --output-dir reports/phase3bb_r35 --reports-dir reports"
        ),
        "copy_service_draft": (
            f"{scp_prefix} reports/phase3bb_r35/{SCHEDULER_SERVICE_NAME}.draft "
            f"{_shell_quote(f'{ssh_target}:{service_tmp}')}"
        ),
        "copy_timer_draft": (
            f"{scp_prefix} reports/phase3bb_r35/{SCHEDULER_TIMER_NAME}.draft "
            f"{_shell_quote(f'{ssh_target}:{timer_tmp}')}"
        ),
        "copy_runner_draft": (
            f"{scp_prefix} reports/phase3bb_r35/{RUNNER_SCRIPT_NAME}.draft "
            f"{_shell_quote(f'{ssh_target}:{runner_tmp}')}"
        ),
        "install_runner_script": (
            f"{ssh_prefix} 'sudo install -D -m 0755 {runner_tmp} {runner_remote}'"
        ),
        "install_service_file": (
            f"{ssh_prefix} 'sudo install -m 0644 {service_tmp} "
            f"/etc/systemd/system/{SCHEDULER_SERVICE_NAME}'"
        ),
        "install_timer_file": (
            f"{ssh_prefix} 'sudo install -m 0644 {timer_tmp} "
            f"/etc/systemd/system/{SCHEDULER_TIMER_NAME}'"
        ),
        "daemon_reload": f"{ssh_prefix} 'sudo systemctl daemon-reload'",
        "enable_timer_no_start": f"{ssh_prefix} 'sudo systemctl enable {SCHEDULER_TIMER_NAME}'",
        "verify_timer_enabled": f"{ssh_prefix} 'systemctl is-enabled {SCHEDULER_TIMER_NAME}'",
        "verify_timer_inactive": (
            f"{ssh_prefix} 'systemctl is-active {SCHEDULER_TIMER_NAME} || true'"
        ),
        "verify_service_inactive": (
            f"{ssh_prefix} 'systemctl is-active {SCHEDULER_SERVICE_NAME} || true'"
        ),
        "verify_r5_status": (
            f"{ssh_prefix} 'cd {app_path} && set -a && . {env_path} && set +a && "
            ".venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5'"
        ),
    }


def _handoff_checks(
    *,
    r11: dict[str, Any],
    r35: dict[str, Any],
    service_text: str,
    timer_text: str,
    runner_text: str,
    handoff_commands: dict[str, str],
    operator_approved: bool,
    r35_age_seconds: float | None,
    r35_max_age_minutes: int,
) -> list[dict[str, Any]]:
    dry_run_decision = r35.get("dry_run_decision") or {}
    all_commands = "\n".join(handoff_commands.values()).lower()
    combined_text = f"{service_text}\n{timer_text}\n{runner_text}\n{all_commands}".lower()
    forbidden_starts = [
        fragment for fragment in FORBIDDEN_SERVICE_START_FRAGMENTS if fragment in all_commands
    ]
    forbidden_trading = [
        fragment for fragment in FORBIDDEN_TRADING_FRAGMENTS if fragment in combined_text
    ]
    max_age_seconds = max(1, r35_max_age_minutes) * 60
    return [
        _check("operator_approved_flag_present", operator_approved, "Operator approved R36."),
        _check("r11_cloud_context_present", bool(r11), "R11 cloud context exists."),
        _check("r35_artifact_present", bool(r35), "R35 no-start dry-run artifact exists."),
        _check(
            "r35_recently_refreshed",
            r35_age_seconds is not None and r35_age_seconds <= max_age_seconds,
            f"R35 artifact age is {r35_age_seconds} seconds.",
        ),
        _check(
            "r35_no_start_dry_run_passed",
            dry_run_decision.get("status") == READY_R35_STATUS
            and bool(dry_run_decision.get("dry_run_passed")),
            f"R35 status is {dry_run_decision.get('status')}.",
        ),
        _check(
            "r35_no_failed_checks",
            dry_run_decision.get("failed_check_count") == 0,
            f"R35 failed checks: {dry_run_decision.get('failed_check_count')}.",
        ),
        _check(
            "scheduler_drafts_present",
            bool(service_text.strip()) and bool(timer_text.strip()) and bool(runner_text.strip()),
            "R35 service/timer/runner drafts are readable.",
        ),
        _check(
            "runner_has_writer_gate",
            "db-writer-monitor --json" in runner_text
            and "writer active; skip writer-gated job" in runner_text.lower(),
            "Runner draft checks db-writer-monitor before writer-capable jobs.",
        ),
        _check(
            "handoff_installs_enable_timer_without_start",
            "systemctl enable" in all_commands
            and "systemctl start" not in all_commands
            and "systemctl enable --now" not in all_commands,
            "Handoff installs and enables timer without starting it.",
        ),
        _check(
            "no_forbidden_service_start_commands",
            not forbidden_starts,
            f"Forbidden service start hits: {', '.join(forbidden_starts) or 'none'}.",
        ),
        _check(
            "no_forbidden_trading_commands",
            not forbidden_trading,
            f"Forbidden trading hits: {', '.join(forbidden_trading) or 'none'}.",
        ),
        _check(
            "does_not_start_duplicate_r5",
            "phase3bc-r5-unattended-start" not in combined_text,
            "Scheduler handoff observes R5 status only.",
        ),
    ]


def _handoff_decision(
    checks: list[dict[str, Any]],
    r35: dict[str, Any],
    operator_approved: bool,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    dry_run_decision = r35.get("dry_run_decision") or {}
    if failed:
        status = "BLOCKED_SCHEDULER_INSTALL_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "HANDOFF_READY_SCHEDULER_INSTALL_ENABLE_NO_START"
        reason = (
            "Operator approval was present and R35 is fresh/passed. The handoff script "
            "defaults to dry-run and installs/enables the scheduler timer only when the "
            "operator supplies the R36 approval token."
        )
    return {
        "status": status,
        "operator_approved": operator_approved,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r35_status": dry_run_decision.get("status"),
        "r5_pid": dry_run_decision.get("r5_pid"),
        "watch_state": dry_run_decision.get("watch_state"),
        "paper_ready_candidates": dry_run_decision.get("paper_ready_candidates"),
        "scheduler_service_name": SCHEDULER_SERVICE_NAME,
        "scheduler_timer_name": SCHEDULER_TIMER_NAME,
        "codex_executed_install": False,
        "codex_executed_enable": False,
        "codex_executed_start": False,
        "handoff_script_default_dry_run": True,
        "handoff_script_can_install_enable_no_start_with_token": not failed,
        "required_execute_env": APPROVAL_ENV_VAR,
        "required_execute_token": APPROVAL_TOKEN,
        "operator_next_command": "bash reports/phase3bb_r36/operator_scheduler_install_handoff.sh",
        "next_codex_step": (
            "Phase 3BB-R37 - Cloud Scheduler Install Verification After Operator Run"
        ),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R36 Cloud Scheduler Install Handoff")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- Operator approved flag: `{decision['operator_approved']}`",
            f"- R35 status: `{decision['r35_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            f"- Service: `{decision['scheduler_service_name']}`",
            f"- Timer: `{decision['scheduler_timer_name']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## What This Phase Did",
            "",
            "- Wrote an operator scheduler install handoff script.",
            "- Kept that script dry-run by default.",
            "- Included install + enable-no-start commands only for explicit operator use.",
            "",
            "## Safety",
            "",
            "- Codex did not copy scheduler files to the cloud.",
            "- Codex did not install, enable, or start the scheduler timer/service.",
            "- Codex did not run refresh jobs.",
            "- Existing R5 was not stopped or duplicated.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R36 Scheduler Install Handoff Detail")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Handoff",
            "",
            "The generated handoff script installs the reviewed R35 scheduler service, "
            "timer, and runner drafts, reloads systemd, and enables the timer without "
            "starting it. A later verification phase should confirm the install before "
            "any start/activation decision.",
            "",
            f"- Decision: `{decision['status']}`",
            f"- Execute env: `{decision['required_execute_env']}`",
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
        f"TOKEN=${{{APPROVAL_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(decision['required_execute_token'])}",
        "",
        "echo '[phase3bb-r36] cloud scheduler install handoff'",
        "echo '[phase3bb-r36] default mode is dry-run; no remote changes occur'",
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
            "  echo '[phase3bb-r36] dry-run command list:'",
            "  printf '  %s\\n' \"${commands[@]}\"",
            "  echo '[phase3bb-r36] no install/enable/start command executed'",
            "  echo '[phase3bb-r36] to execute install+enable-no-start, set:'",
            f"  echo \"  {APPROVAL_ENV_VAR}=$REQUIRED bash $0\"",
            "  exit 0",
            "fi",
            "",
            "echo '[phase3bb-r36] approval token accepted'",
            "echo '[phase3bb-r36] running scheduler install + enable-no-start handoff'",
            "for command in \"${commands[@]}\"; do",
            "  echo \"+ $command\"",
            "  bash -lc \"$command\"",
            "done",
            "echo '[phase3bb-r36] handoff commands completed'",
            "echo '[phase3bb-r36] verify with Phase 3BB-R37 next'",
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
            "# Default dry-run. This prints the scheduler install commands only.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R36 Next Actions")
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
            "This prints the command list only. To execute the scheduler "
            "install+enable-no-start handoff, rerun it with "
            f"`{APPROVAL_ENV_VAR}={APPROVAL_TOKEN}`.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            f"- Do not run `systemctl start {SCHEDULER_TIMER_NAME}` yet.",
            f"- Do not run `systemctl start {SCHEDULER_SERVICE_NAME}` yet.",
            "- Do not start a duplicate R5 watcher.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifact_age_seconds(payload: dict[str, Any], now: Any) -> float | None:
    parsed = parse_datetime(payload.get("generated_at"))
    if parsed is None:
        return None
    return max(0.0, round((now - parsed).total_seconds(), 3))


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


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
