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
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import DEFAULT_REPORTS_DIR
from kalshi_predictor.utils.time import parse_datetime, utc_now

PHASE3BB_R29_VERSION = "phase3bb_r29_cloud_ui_private_access_install_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r29")
DEFAULT_R28_MAX_AGE_MINUTES = 60
APPROVAL_ENV_VAR = "PHASE3BB_R29_EXECUTE"
APPROVAL_TOKEN = "I_APPROVE_R29_PRIVATE_ACCESS_INSTALL"
READY_R28_STATUS = "PRIVATE_ACCESS_OPERATOR_REVIEW_READY_NO_INSTALL"
READY_SELECTED_OPTION = "PRIVATE_VPN_OR_TAILSCALE"

FORBIDDEN_PUBLIC_OR_UI_FRAGMENTS = (
    "ufw allow",
    "nginx",
    "tailscale funnel",
    "--advertise-routes",
    "--accept-routes",
    "--ssh",
    "0.0.0.0:8080",
    "--host 0.0.0.0",
    "systemctl start kalshi-ui.service",
    "systemctl restart kalshi-ui.service",
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
class Phase3BBR29CloudUiPrivateAccessInstallHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    checks_csv_path: Path
    operator_handoff_script_path: Path
    operator_next_command_path: Path
    install_plan_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r29_cloud_ui_private_access_install_handoff_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r28_max_age_minutes: int = DEFAULT_R28_MAX_AGE_MINUTES,
) -> Phase3BBR29CloudUiPrivateAccessInstallHandoffArtifacts:
    payload = build_phase3bb_r29_cloud_ui_private_access_install_handoff(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        operator_approved=operator_approved,
        r28_max_age_minutes=r28_max_age_minutes,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_private_access_install_handoff.md"
    json_path = output_dir / "cloud_ui_private_access_install_handoff.json"
    checks_csv_path = output_dir / "private_access_handoff_checks.csv"
    operator_handoff_script_path = output_dir / "operator_private_access_install_handoff.sh"
    operator_next_command_path = output_dir / "operator_next_command.sh"
    install_plan_path = output_dir / "TAILSCALE_PRIVATE_ACCESS_INSTALL_PLAN.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(checks_csv_path, payload["private_access_handoff_checks"])
    operator_handoff_script_path.write_text(_render_handoff_script(payload), encoding="utf-8")
    _mark_executable(operator_handoff_script_path)
    operator_next_command_path.write_text(_render_operator_next_command(payload), encoding="utf-8")
    _mark_executable(operator_next_command_path)
    install_plan_path.write_text(_render_install_plan(payload), encoding="utf-8")
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            checks_csv_path,
            operator_handoff_script_path,
            operator_next_command_path,
            install_plan_path,
            next_actions_path,
        ],
    )
    return Phase3BBR29CloudUiPrivateAccessInstallHandoffArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        checks_csv_path=checks_csv_path,
        operator_handoff_script_path=operator_handoff_script_path,
        operator_next_command_path=operator_next_command_path,
        install_plan_path=install_plan_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r29_cloud_ui_private_access_install_handoff(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    operator_approved: bool = False,
    r28_max_age_minutes: int = DEFAULT_R28_MAX_AGE_MINUTES,
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
        "command": "kalshi-bot phase3bb-r29-cloud-ui-private-access-install-handoff",
        "argv": command_args or [],
    }
    r28_path = reports_dir / "phase3bb_r28" / "cloud_ui_private_access_operator_review.json"
    r27_path = reports_dir / "phase3bb_r27" / "cloud_ui_private_access_auth_draft.json"
    r26_path = reports_dir / "phase3bb_r26" / "cloud_ui_access_control_decision.json"
    r24_path = reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json"
    r20_path = reports_dir / "phase3bb_r20" / "cloud_ui_service_plan.json"
    r28 = _read_json(r28_path)
    r27 = _read_json(r27_path)
    r26 = _read_json(r26_path)
    r24 = _read_json(r24_path)
    r20 = _read_json(r20_path)
    r28_age_seconds = _artifact_age_seconds(r28, now)
    target = _cloud_target(r20, r24)
    selected_plan = r28.get("selected_private_access_plan") or {}
    commands = _handoff_commands(target=target)
    checks = _handoff_checks(
        r28=r28,
        r27=r27,
        r26=r26,
        r24=r24,
        selected_plan=selected_plan,
        commands=commands,
        operator_approved=operator_approved,
        r28_age_seconds=r28_age_seconds,
        r28_max_age_minutes=r28_max_age_minutes,
    )
    decision = _decision(
        checks,
        r28=r28,
        r27=r27,
        r26=r26,
        r24=r24,
        selected_plan=selected_plan,
        operator_approved=operator_approved,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "handoff_only": True,
        "script_default_dry_run": True,
        "no_public_exposure": True,
        "no_nginx_install": True,
        "no_firewall_change": True,
        "service_files_written_to_system": False,
        "operator_handoff_script_written": True,
        "private_access_installed_by_codex": False,
        "tailscale_commands_executed_by_codex": 0,
        "systemctl_commands_executed": 0,
        "ssh_commands_executed": 0,
        "remote_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
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
    }
    return {
        **metadata,
        "phase": "3BB-R29-CLOUD-UI-PRIVATE-ACCESS-INSTALL-HANDOFF",
        "phase_version": PHASE3BB_R29_VERSION,
        "mode": "PAPER_READ_ONLY_PRIVATE_ACCESS_INSTALL_HANDOFF",
        "reports_dir": str(reports_dir),
        "r28_artifact_path": str(r28_path),
        "r27_artifact_path": str(r27_path),
        "r26_artifact_path": str(r26_path),
        "r24_artifact_path": str(r24_path),
        "r20_artifact_path": str(r20_path),
        "r28_age_seconds": r28_age_seconds,
        "r28_max_age_minutes": r28_max_age_minutes,
        "operator_approved": operator_approved,
        "cloud_target": target,
        "selected_private_access_plan": selected_plan,
        "private_access_handoff_checks": checks,
        "private_access_handoff_commands": commands,
        "private_access_handoff_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "source_docs": [
            "https://tailscale.com/docs/install/linux",
            "https://tailscale.com/docs/reference/tailscale-cli/serve",
        ],
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


def _cloud_target(r20: dict[str, Any], r24: dict[str, Any]) -> dict[str, Any]:
    ui_plan = r20.get("ui_service_plan") or {}
    r18_target = ((r24.get("r18_preflight") or {}).get("cloud_target")) or {}
    verification = r24.get("verification_decision") or {}
    return {
        "ssh_target": ui_plan.get("ssh_target")
        or r18_target.get("ssh_target")
        or "kalshi@159.65.35.72",
        "identity_file": ui_plan.get("identity_file")
        or r18_target.get("identity_file")
        or "/home/james/.ssh/id_ed25519_do",
        "app_path": ui_plan.get("remote_app_path")
        or r18_target.get("app_path")
        or "/opt/kalshi-predictive-bot",
        "env_path": ui_plan.get("remote_env_path")
        or r18_target.get("env_path")
        or "/etc/kalshi-bot/kalshi-bot.env",
        "ui_bind_host": ui_plan.get("ui_bind_host") or "127.0.0.1",
        "ui_bind_port": int(ui_plan.get("ui_bind_port") or 8080),
        "ui_service_name": ui_plan.get("ui_service_name") or "kalshi-ui.service",
        "ssh_tunnel_command": ui_plan.get("ssh_tunnel_command")
        or verification.get("ssh_tunnel_command"),
    }


def _handoff_commands(*, target: dict[str, Any]) -> dict[str, str]:
    ssh_target = str(target["ssh_target"])
    identity_file = str(target["identity_file"])
    app_path = str(target["app_path"])
    env_path = str(target["env_path"])
    ui_host = str(target["ui_bind_host"])
    ui_port = int(target["ui_bind_port"])
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    install_inner = (
        "command -v tailscale >/dev/null 2>&1 "
        "|| curl -fsSL https://tailscale.com/install.sh | sudo sh"
    )
    return {
        "refresh_r28_local": (
            "timeout 180 .venv/bin/kalshi-bot "
            "phase3bb-r28-cloud-ui-private-access-operator-review "
            "--output-dir reports/phase3bb_r28 --reports-dir reports"
        ),
        "remote_preflight": _remote_command(
            ssh_prefix,
            "systemctl is-active kalshi-ui.service && "
            f"ss -ltnp 2>/dev/null | grep '{ui_host}:{ui_port}' >/dev/null",
            timeout_seconds=45,
        ),
        "install_tailscale_if_missing": _remote_command(
            ssh_prefix,
            install_inner,
            timeout_seconds=300,
        ),
        "enable_tailscaled": _remote_command(
            ssh_prefix,
            "sudo systemctl enable --now tailscaled",
            timeout_seconds=120,
        ),
        "tailscale_login_window": _remote_command(
            ssh_prefix,
            (
                "sudo tailscale status >/dev/null 2>&1 || "
                "timeout 90 sudo tailscale up --accept-dns=false --hostname=kalshi-bot-01 "
                "|| true"
            ),
            timeout_seconds=120,
        ),
        "serve_local_ui_to_tailnet_if_authenticated": _remote_command(
            ssh_prefix,
            (
                "sudo tailscale status >/dev/null 2>&1 "
                f"&& sudo tailscale serve --bg http://{ui_host}:{ui_port} "
                "|| echo 'tailscale not authenticated yet; finish login then rerun R30'"
            ),
            timeout_seconds=120,
        ),
        "tailscale_status": _remote_command(
            ssh_prefix,
            "sudo tailscale status || true; sudo tailscale serve status || true",
            timeout_seconds=120,
        ),
        "cloud_ui_status_after_private_access": _remote_command(
            ssh_prefix,
            (
                f"cd {app_path} && set -a && . {env_path} && set +a && "
                ".venv/bin/kalshi-bot phase3bb-r24-cloud-ui-start-tunnel-verification "
                "--output-dir reports/phase3bb_r24 --reports-dir reports"
            ),
            timeout_seconds=180,
        ),
    }


def _remote_command(ssh_prefix: str, command: str, *, timeout_seconds: int) -> str:
    escaped = command.replace("'", "'\"'\"'")
    return f"{ssh_prefix} 'timeout {timeout_seconds} bash -lc '\\''{escaped}'\\'''"


def _handoff_checks(
    *,
    r28: dict[str, Any],
    r27: dict[str, Any],
    r26: dict[str, Any],
    r24: dict[str, Any],
    selected_plan: dict[str, Any],
    commands: dict[str, str],
    operator_approved: bool,
    r28_age_seconds: float | None,
    r28_max_age_minutes: int,
) -> list[dict[str, Any]]:
    r28_decision = r28.get("operator_review_decision") or {}
    r27_decision = r27.get("private_access_decision") or {}
    r26_decision = r26.get("access_control_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    all_commands = "\n".join(commands.values()).lower()
    selected_option = str(
        selected_plan.get("option") or r28_decision.get("selected_option") or ""
    )
    forbidden_public = [
        fragment for fragment in FORBIDDEN_PUBLIC_OR_UI_FRAGMENTS if fragment in all_commands
    ]
    forbidden_trading = [
        fragment for fragment in FORBIDDEN_TRADING_FRAGMENTS if fragment in all_commands
    ]
    max_age_seconds = max(1, r28_max_age_minutes) * 60
    return [
        _check("operator_approved_flag_present", operator_approved, "Operator approved R29."),
        _check("r28_artifact_present", bool(r28), "R28 operator review artifact exists."),
        _check("r27_artifact_present", bool(r27), "R27 private access draft artifact exists."),
        _check(
            "r28_recently_refreshed",
            r28_age_seconds is not None and r28_age_seconds <= max_age_seconds,
            f"R28 artifact age is {r28_age_seconds} seconds.",
        ),
        _check(
            "r28_review_ready",
            r28_decision.get("status") == READY_R28_STATUS,
            f"R28 status is {r28_decision.get('status')}.",
        ),
        _check(
            "r28_no_failed_checks",
            r28_decision.get("failed_check_count") == 0,
            f"R28 failed checks: {r28_decision.get('failed_check_count')}.",
        ),
        _check(
            "selected_private_vpn_plan",
            selected_option == READY_SELECTED_OPTION,
            f"Selected option is {selected_option}.",
        ),
        _check(
            "selected_plan_not_public",
            selected_plan.get("public_exposure") == "NONE",
            f"Selected public exposure is {selected_plan.get('public_exposure')}.",
        ),
        _check(
            "r26_public_https_still_blocked",
            r26_decision.get("public_https_allowed_now") is False,
            f"R26 public_https_allowed_now={r26_decision.get('public_https_allowed_now')}.",
        ),
        _check(
            "r24_ui_running_localhost",
            r24_decision.get("status") == "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
            f"R24 status is {r24_decision.get('status')}.",
        ),
        _check(
            "r27_matches_private_vpn",
            r27_decision.get("selected_option") == READY_SELECTED_OPTION,
            f"R27 selected option is {r27_decision.get('selected_option')}.",
        ),
        _check(
            "commands_include_tailscale",
            "tailscale" in all_commands,
            "Handoff commands include the private access client.",
        ),
        _check(
            "commands_include_serve_localhost_only",
            "tailscale serve --bg http://127.0.0.1:8080" in all_commands,
            "Handoff serves only the existing localhost UI.",
        ),
        _check(
            "commands_are_timeout_bounded",
            all("timeout " in command for command in commands.values()),
            "Every executable command is timeout-bounded.",
        ),
        _check(
            "no_public_or_ui_start_fragments",
            not forbidden_public,
            f"Forbidden public/UI fragments: {', '.join(forbidden_public) or 'none'}.",
        ),
        _check(
            "no_forbidden_trading_commands",
            not forbidden_trading,
            f"Forbidden trading fragments: {', '.join(forbidden_trading) or 'none'}.",
        ),
    ]


def _decision(
    checks: list[dict[str, Any]],
    *,
    r28: dict[str, Any],
    r27: dict[str, Any],
    r26: dict[str, Any],
    r24: dict[str, Any],
    selected_plan: dict[str, Any],
    operator_approved: bool,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r28_decision = r28.get("operator_review_decision") or {}
    r27_decision = r27.get("private_access_decision") or {}
    r26_decision = r26.get("access_control_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    if failed:
        status = "BLOCKED_PRIVATE_ACCESS_INSTALL_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
    else:
        status = "HANDOFF_READY_PRIVATE_ACCESS_INSTALL_DRY_RUN"
        reason = (
            "Operator approval was present, R28 is fresh, and the selected access "
            "plan remains private VPN/Tailscale with no public HTTPS/firewall/nginx "
            "changes. The generated script defaults to dry-run and only runs with "
            "the R29 approval token."
        )
    return {
        "status": status,
        "operator_approved": operator_approved,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "selected_option": selected_plan.get("option") or r28_decision.get("selected_option"),
        "selected_option_status": selected_plan.get("status"),
        "r28_status": r28_decision.get("status"),
        "r27_status": r27_decision.get("status"),
        "r26_status": r26_decision.get("status"),
        "r24_status": r24_decision.get("status"),
        "r5_pid": r28_decision.get("r5_pid")
        or r27_decision.get("r5_pid")
        or r26_decision.get("r5_pid")
        or r24_decision.get("r5_pid"),
        "codex_executed_private_access_install": False,
        "codex_executed_tailscale_commands": False,
        "codex_executed_firewall_or_nginx_change": False,
        "public_https_allowed_now": False,
        "firewall_change_allowed_now": False,
        "handoff_script_default_dry_run": True,
        "handoff_script_can_install_with_token": not failed,
        "requires_operator_tailnet_login": True,
        "required_execute_env": APPROVAL_ENV_VAR,
        "required_execute_token": APPROVAL_TOKEN,
        "operator_next_command": (
            "bash reports/phase3bb_r29/operator_private_access_install_handoff.sh"
        ),
        "next_codex_step": (
            "Phase 3BB-R30 - Cloud UI Private Access Install Verification "
            "After Operator Run"
        ),
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R29 Cloud UI Private Access Handoff")
    decision = payload["private_access_handoff_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- Operator approved flag: `{decision['operator_approved']}`",
            f"- Selected option: `{decision['selected_option']}`",
            f"- R28 status: `{decision['r28_status']}`",
            f"- R24 status: `{decision['r24_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## What This Phase Did",
            "",
            "- Wrote an operator private-access install handoff script.",
            "- Kept that script dry-run by default.",
            "- Kept the cloud UI bound to localhost/private access only.",
            "- Left nginx, firewall, public HTTPS, paper trades, and exchange orders untouched.",
            "",
            "## Next Operator Command",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R29 Private Access Handoff Detail")
    decision = payload["private_access_handoff_decision"]
    selected = payload["selected_private_access_plan"]
    lines.extend(
        [
            "",
            "## Handoff",
            "",
            "The generated script prepares a private VPN/Tailscale path for the existing "
            "localhost-only cloud UI. It does not open public web ports, does not install "
            "nginx, and does not start or stop R5.",
            "",
            f"- Decision: `{decision['status']}`",
            f"- Execute env: `{decision['required_execute_env']}`",
            f"- Execute token: `{decision['required_execute_token']}`",
            f"- Selected plan: `{selected.get('option')}`",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["private_access_handoff_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Handoff Commands", ""])
    for name, command in payload["private_access_handoff_commands"].items():
        lines.extend([f"### {name}", "", "```bash", command, "```", ""])
    return "\n".join(lines) + "\n"


def _render_handoff_script(payload: dict[str, Any]) -> str:
    commands = payload["private_access_handoff_commands"]
    decision = payload["private_access_handoff_decision"]
    executable_commands = list(commands.values())
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TOKEN=${{{APPROVAL_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(decision['required_execute_token'])}",
        "",
        "echo '[phase3bb-r29] cloud UI private access install handoff'",
        "echo '[phase3bb-r29] default mode is dry-run; no remote changes occur'",
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
            "  echo '[phase3bb-r29] dry-run command list:'",
            "  printf '  %s\\n' \"${commands[@]}\"",
            "  echo '[phase3bb-r29] no private access install command executed'",
            "  echo '[phase3bb-r29] to execute private access handoff, set:'",
            f"  echo \"  {APPROVAL_ENV_VAR}=$REQUIRED bash $0\"",
            "  exit 0",
            "fi",
            "",
            "echo '[phase3bb-r29] approval token accepted'",
            "echo '[phase3bb-r29] running private access handoff'",
            "for command in \"${commands[@]}\"; do",
            "  echo \"+ $command\"",
            "  bash -lc \"$command\"",
            "done",
            "echo '[phase3bb-r29] handoff commands completed'",
            "echo '[phase3bb-r29] verify with Phase 3BB-R30 next'",
            "",
        ]
    )
    return "\n".join(lines)


def _render_operator_next_command(payload: dict[str, Any]) -> str:
    decision = payload["private_access_handoff_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Default dry-run. This prints the private access install commands only.",
            decision["operator_next_command"],
            "",
        ]
    )


def _render_install_plan(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Tailscale Private Access Install Plan")
    decision = payload["private_access_handoff_decision"]
    target = payload["cloud_target"]
    lines.extend(
        [
            "",
            "## Selected Path",
            "",
            "- Provider family: `Tailscale/private VPN`",
            "- Public exposure: `NONE`",
            f"- SSH target: `{target['ssh_target']}`",
            f"- UI backend: `http://{target['ui_bind_host']}:{target['ui_bind_port']}`",
            f"- Approval env: `{decision['required_execute_env']}`",
            f"- Approval token: `{decision['required_execute_token']}`",
            "",
            "## Expected Operator Flow",
            "",
            "1. Run the generated handoff script once without the token and review commands.",
            "2. Run the same script with the R29 approval token.",
            "3. If Tailscale prints a login URL, complete that login in the browser.",
            "4. Run the next R30 verification phase before relying on the private URL.",
            "",
            "## Explicit Non-Goals",
            "",
            "- No public HTTPS.",
            "- No nginx.",
            "- No firewall opening.",
            "- No paper/live/demo trading.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R29 Next Actions")
    decision = payload["private_access_handoff_decision"]
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
            "This prints the command list only. To execute the private access handoff, "
            f"rerun it with `{APPROVAL_ENV_VAR}={APPROVAL_TOKEN}`.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly.",
            "- Do not install nginx or open firewall ports.",
            "- Do not stop or duplicate R5.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["check", "passed", "detail"])
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
