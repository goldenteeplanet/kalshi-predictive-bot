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

PHASE3BB_R22_VERSION = "phase3bb_r22_cloud_ui_install_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r22")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_R21_MAX_AGE_MINUTES = 30
DEFAULT_UI_SERVICE_NAME = "kalshi-ui.service"
APPROVAL_ENV_VAR = "PHASE3BB_R22_EXECUTE"
APPROVAL_TOKEN = "I_APPROVE_R22_UI_INSTALL"

FORBIDDEN_UI_START_FRAGMENTS = (
    "systemctl restart",
    "systemctl start",
    "systemctl try-restart",
    "systemctl enable --now",
    "systemctl reenable",
    "ufw allow",
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
class Phase3BBR22CloudUiInstallHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    handoff_checks_path: Path
    operator_handoff_script_path: Path
    operator_next_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r22_cloud_ui_install_handoff_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r21_max_age_minutes: int = DEFAULT_R21_MAX_AGE_MINUTES,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
) -> Phase3BBR22CloudUiInstallHandoffArtifacts:
    payload = build_phase3bb_r22_cloud_ui_install_handoff(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        operator_approved=operator_approved,
        r21_max_age_minutes=r21_max_age_minutes,
        ui_service_name=ui_service_name,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_install_handoff.md"
    json_path = output_dir / "cloud_ui_install_handoff.json"
    handoff_checks_path = output_dir / "handoff_checks.csv"
    operator_handoff_script_path = output_dir / "operator_ui_install_handoff.sh"
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
    return Phase3BBR22CloudUiInstallHandoffArtifacts(
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


def build_phase3bb_r22_cloud_ui_install_handoff(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r21_max_age_minutes: int = DEFAULT_R21_MAX_AGE_MINUTES,
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
        "command": "kalshi-bot phase3bb-r22-cloud-ui-install-handoff",
        "argv": command_args or [],
    }
    r20_path = reports_dir / "phase3bb_r20" / "cloud_ui_service_plan.json"
    r21_path = reports_dir / "phase3bb_r21" / "cloud_ui_install_review.json"
    r20 = _read_json(r20_path)
    r21 = _read_json(r21_path)
    r20_dir = reports_dir / "phase3bb_r20"
    service_draft_path = r20_dir / f"{ui_service_name}.draft"
    nginx_draft_path = r20_dir / "kalshi-ui.nginx.draft"
    service_text = _read_text(service_draft_path)
    nginx_text = _read_text(nginx_draft_path)
    r21_age_seconds = _artifact_age_seconds(r21, now)
    target = _cloud_target(r20, r21)
    ui_plan = r20.get("ui_service_plan") or r21.get("ui_service_plan") or {}
    handoff_commands = _handoff_commands(
        target=target,
        ui_service_name=ui_service_name,
        ssh_tunnel_command=str(ui_plan.get("ssh_tunnel_command") or ""),
    )
    handoff_checks = _handoff_checks(
        r20=r20,
        r21=r21,
        service_text=service_text,
        nginx_text=nginx_text,
        handoff_commands=handoff_commands,
        operator_approved=operator_approved,
        r21_age_seconds=r21_age_seconds,
        r21_max_age_minutes=r21_max_age_minutes,
    )
    decision = _handoff_decision(
        handoff_checks,
        r20,
        r21,
        operator_approved,
        ui_service_name=ui_service_name,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_service_start": True,
        "no_live_or_demo_orders": True,
        "no_nginx_install": True,
        "no_firewall_change": True,
        "service_files_written_to_system": False,
        "operator_handoff_script_written": True,
        "operator_handoff_script_default_dry_run": True,
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
        "phase": "3BB-R22-CLOUD-UI-INSTALL-HANDOFF",
        "phase_version": PHASE3BB_R22_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_INSTALL_HANDOFF",
        "reports_dir": str(reports_dir),
        "r20_artifact_path": str(r20_path),
        "r21_artifact_path": str(r21_path),
        "service_draft_path": str(service_draft_path),
        "nginx_draft_path": str(nginx_draft_path),
        "r20_context_available": bool(r20),
        "r21_context_available": bool(r21),
        "r21_age_seconds": r21_age_seconds,
        "r21_max_age_minutes": r21_max_age_minutes,
        "operator_approved": operator_approved,
        "cloud_target": target,
        "ui_service_plan": ui_plan,
        "install_review_decision": r21.get("install_review_decision") or {},
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


def _cloud_target(r20: dict[str, Any], r21: dict[str, Any]) -> dict[str, Any]:
    ui_plan = r20.get("ui_service_plan") or r21.get("ui_service_plan") or {}
    return {
        "ssh_target": ui_plan.get("ssh_target") or "kalshi@159.65.35.72",
        "identity_file": ui_plan.get("identity_file") or "/home/james/.ssh/id_ed25519_do",
        "app_path": ui_plan.get("remote_app_path") or "/opt/kalshi-predictive-bot",
        "env_path": ui_plan.get("remote_env_path") or "/etc/kalshi-bot/kalshi-bot.env",
        "db_path": ui_plan.get("remote_db_path") or "/var/lib/kalshi-bot/kalshi_phase1.db",
        "reports_path": ui_plan.get("remote_reports_path")
        or "/opt/kalshi-predictive-bot/reports",
    }


def _handoff_commands(
    *,
    target: dict[str, Any],
    ui_service_name: str,
    ssh_tunnel_command: str,
) -> dict[str, str]:
    ssh_target = str(target["ssh_target"])
    identity_file = str(target["identity_file"])
    app_path = str(target["app_path"] or "/opt/kalshi-predictive-bot")
    env_path = str(target["env_path"] or "/etc/kalshi-bot/kalshi-bot.env")
    service_tmp = f"/tmp/{ui_service_name}"
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    scp_prefix = f"scp -i {_shell_quote(identity_file)}"
    commands = {
        "refresh_r21": (
            ".venv/bin/kalshi-bot phase3bb-r21-cloud-ui-install-review "
            "--output-dir reports/phase3bb_r21 --reports-dir reports"
        ),
        "copy_ui_service_draft": (
            f"{scp_prefix} reports/phase3bb_r20/{ui_service_name}.draft "
            f"{_shell_quote(f'{ssh_target}:{service_tmp}')}"
        ),
        "install_ui_service_file": (
            f"{ssh_prefix} 'sudo install -m 0644 {service_tmp} "
            f"/etc/systemd/system/{ui_service_name}'"
        ),
        "daemon_reload": f"{ssh_prefix} 'sudo systemctl daemon-reload'",
        "enable_no_start": f"{ssh_prefix} 'sudo systemctl enable {ui_service_name}'",
        "verify_enabled": f"{ssh_prefix} 'systemctl is-enabled {ui_service_name}'",
        "verify_inactive": f"{ssh_prefix} 'systemctl is-active {ui_service_name} || true'",
        "verify_r5_status": (
            f"{ssh_prefix} 'cd {app_path} && set -a && . {env_path} && set +a && "
            ".venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5'"
        ),
    }
    if ssh_tunnel_command:
        commands["ssh_tunnel_after_start_later"] = ssh_tunnel_command
    return commands


def _handoff_checks(
    *,
    r20: dict[str, Any],
    r21: dict[str, Any],
    service_text: str,
    nginx_text: str,
    handoff_commands: dict[str, str],
    operator_approved: bool,
    r21_age_seconds: float | None,
    r21_max_age_minutes: int,
) -> list[dict[str, Any]]:
    ui_plan = r20.get("ui_service_plan") or r21.get("ui_service_plan") or {}
    install_review = r21.get("install_review_decision") or {}
    all_commands = "\n".join(handoff_commands.values()).lower()
    combined_text = f"{service_text}\n{nginx_text}\n{all_commands}".lower()
    forbidden_starts = [
        fragment for fragment in FORBIDDEN_UI_START_FRAGMENTS if fragment in all_commands
    ]
    forbidden_trading = [
        fragment for fragment in FORBIDDEN_TRADING_FRAGMENTS if fragment in combined_text
    ]
    max_age_seconds = max(1, r21_max_age_minutes) * 60
    return [
        _check("operator_approved_flag_present", operator_approved, "Operator approved R22."),
        _check("r20_artifact_present", bool(r20), "R20 UI service plan artifact exists."),
        _check("r21_artifact_present", bool(r21), "R21 UI install review artifact exists."),
        _check(
            "r21_recently_refreshed",
            r21_age_seconds is not None and r21_age_seconds <= max_age_seconds,
            f"R21 artifact age is {r21_age_seconds} seconds.",
        ),
        _check(
            "r21_install_review_ready",
            install_review.get("status") == "READY_FOR_OPERATOR_UI_INSTALL_REVIEW_NO_START",
            f"R21 status is {install_review.get('status')}.",
        ),
        _check(
            "r21_no_failed_checks",
            install_review.get("failed_check_count") == 0,
            f"R21 failed checks: {install_review.get('failed_check_count')}.",
        ),
        _check(
            "r20_draft_ready",
            ui_plan.get("status") == "DRAFT_READY_FOR_REVIEW",
            f"R20 status is {ui_plan.get('status')}.",
        ),
        _check(
            "r20_r5_systemd_owned",
            ui_plan.get("r18_status") == "SYSTEMD_OWNS_R5",
            f"R18 status in R20 is {ui_plan.get('r18_status')}.",
        ),
        _check(
            "ui_service_draft_present",
            bool(service_text.strip()),
            "R20 UI service draft is readable.",
        ),
        _check(
            "ui_service_localhost_only",
            "--host 127.0.0.1" in service_text and "--host 0.0.0.0" not in service_text,
            "UI service remains localhost-only.",
        ),
        _check(
            "ui_service_read_only_flags",
            "UI_READ_ONLY=true" in service_text
            and "EXECUTION_ENABLED=false" in service_text
            and "EXECUTION_KILL_SWITCH=true" in service_text,
            "UI service pins read-only execution flags.",
        ),
        _check(
            "nginx_public_exposure_deferred",
            "deferred" in nginx_text.lower() or "do not install" in nginx_text.lower(),
            "Nginx draft remains explicitly deferred.",
        ),
        _check(
            "handoff_installs_enable_without_start",
            "systemctl enable" in all_commands and "systemctl start" not in all_commands,
            "Handoff contains install + enable-no-start commands only.",
        ),
        _check(
            "no_forbidden_ui_start_commands",
            not forbidden_starts,
            f"Forbidden start/public hits: {', '.join(forbidden_starts) or 'none'}.",
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
    r20: dict[str, Any],
    r21: dict[str, Any],
    operator_approved: bool,
    *,
    ui_service_name: str,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    ui_plan = r20.get("ui_service_plan") or r21.get("ui_service_plan") or {}
    install_review = r21.get("install_review_decision") or {}
    if failed:
        status = "BLOCKED_UI_INSTALL_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "HANDOFF_READY_UI_INSTALL_ENABLE_NO_START"
        reason = (
            "Operator approval was present, R21 is fresh, and the UI draft remains "
            "localhost-only/read-only. The handoff script defaults to dry-run and "
            "installs/enables the UI service only when the operator supplies the "
            "R22 approval token."
        )
    return {
        "status": status,
        "operator_approved": operator_approved,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r20_status": ui_plan.get("status"),
        "r21_status": install_review.get("status"),
        "r18_status": ui_plan.get("r18_status"),
        "r5_pid": ui_plan.get("r5_pid") or install_review.get("r5_pid"),
        "ui_service_name": ui_service_name,
        "ssh_tunnel_command": ui_plan.get("ssh_tunnel_command"),
        "codex_executed_install": False,
        "codex_executed_enable": False,
        "codex_executed_start": False,
        "codex_executed_nginx_install": False,
        "handoff_script_default_dry_run": True,
        "handoff_script_can_install_enable_no_start_with_token": not failed,
        "required_execute_env": APPROVAL_ENV_VAR,
        "required_execute_token": APPROVAL_TOKEN,
        "operator_next_command": "bash reports/phase3bb_r22/operator_ui_install_handoff.sh",
        "next_codex_step": (
            "Phase 3BB-R23 - Cloud UI Install Verification After Operator Run"
        ),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R22 Cloud UI Install Handoff")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- Operator approved flag: `{decision['operator_approved']}`",
            f"- R18 status: `{decision['r18_status']}`",
            f"- R20 status: `{decision['r20_status']}`",
            f"- R21 status: `{decision['r21_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## What This Phase Did",
            "",
            "- Wrote an operator UI install handoff script.",
            "- Kept that script dry-run by default.",
            "- Included UI install + enable-no-start commands only for explicit operator use.",
            "- Left nginx and public exposure deferred.",
            "",
            "## Safety",
            "",
            "- Codex did not copy service files to the cloud.",
            "- Codex did not install, enable, or start the UI service.",
            "- Codex did not install nginx or open firewall ports.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R22 UI Install Handoff Detail")
    decision = payload["handoff_decision"]
    lines.extend(
        [
            "",
            "## Handoff",
            "",
            "The generated handoff script installs the reviewed R20 UI service draft, "
            "reloads systemd, and enables the UI service without starting it. Public "
            "nginx exposure remains deferred; access should stay SSH-tunnel-first.",
            "",
            f"- Decision: `{decision['status']}`",
            f"- Execute env: `{decision['required_execute_env']}`",
            f"- Execute token: `{decision['required_execute_token']}`",
            f"- SSH tunnel after a later start: `{decision.get('ssh_tunnel_command')}`",
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
    executable_commands = [
        command for name, command in commands.items() if not name.endswith("_later")
    ]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TOKEN=${{{APPROVAL_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(decision['required_execute_token'])}",
        "",
        "echo '[phase3bb-r22] cloud UI install handoff'",
        "echo '[phase3bb-r22] default mode is dry-run; no remote changes occur'",
        "",
        "commands=(",
    ]
    for command in executable_commands:
        lines.append(f"  {_shell_quote(command)}")
    lines.extend(
        [
            ")",
            "",
            "if [[ \"$TOKEN\" != \"$REQUIRED\" ]]; then",
            "  echo '[phase3bb-r22] dry-run command list:'",
            "  printf '  %s\\n' \"${commands[@]}\"",
            "  echo '[phase3bb-r22] no install/enable/start command executed'",
            "  echo '[phase3bb-r22] to execute install+enable-no-start, set:'",
            f"  echo \"  {APPROVAL_ENV_VAR}=$REQUIRED bash $0\"",
            "  exit 0",
            "fi",
            "",
            "echo '[phase3bb-r22] approval token accepted'",
            "echo '[phase3bb-r22] running UI install + enable-no-start handoff'",
            "for command in \"${commands[@]}\"; do",
            "  echo \"+ $command\"",
            "  bash -lc \"$command\"",
            "done",
            "echo '[phase3bb-r22] handoff commands completed'",
            "echo '[phase3bb-r22] verify with Phase 3BB-R23 next'",
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
            "# Default dry-run. This prints the operator UI install commands only.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R22 Next Actions")
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
            "This prints the command list only. To execute the UI install+enable-no-start "
            f"handoff, rerun it with `{APPROVAL_ENV_VAR}={APPROVAL_TOKEN}`.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not run `systemctl start kalshi-ui.service` yet.",
            "- Do not install nginx or open firewall ports yet.",
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
