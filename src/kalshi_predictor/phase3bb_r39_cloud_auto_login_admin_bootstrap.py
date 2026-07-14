from __future__ import annotations

import csv
import json
import shlex
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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    RUNNER_SCRIPT_NAME,
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R39_VERSION = "phase3bb_r39_cloud_auto_login_admin_bootstrap_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r39")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 30
SSH_ALIAS = "kalshi-cloud"
SSH_CONFIG_ENV_VAR = "PHASE3BB_R39_SSH_CONFIG"
SSH_CONFIG_TOKEN = "I_APPROVE_R39_SSH_CONFIG"
ROOT_BOOTSTRAP_REMOTE_PATH = "/tmp/phase3bb_r39_admin_bootstrap.sh"


@dataclass(frozen=True)
class Phase3BBR39CloudAutoLoginAdminBootstrapArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    ssh_config_handoff_path: Path
    root_bootstrap_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r39_cloud_auto_login_admin_bootstrap_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR39CloudAutoLoginAdminBootstrapArtifacts:
    payload = build_phase3bb_r39_cloud_auto_login_admin_bootstrap(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_auto_login_admin_bootstrap.md"
    json_path = output_dir / "cloud_auto_login_admin_bootstrap.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "bootstrap_checks.csv"
    ssh_config_handoff_path = output_dir / "operator_ssh_config_handoff.sh"
    root_bootstrap_path = output_dir / "root_console_admin_bootstrap.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_probe_csv(probe_csv_path, payload["remote_probe_results"])
    _write_checks_csv(checks_csv_path, payload["bootstrap_checks"])
    ssh_config_handoff_path.write_text(_render_ssh_config_handoff(payload), encoding="utf-8")
    _mark_executable(ssh_config_handoff_path)
    root_bootstrap_path.write_text(_render_root_bootstrap(payload), encoding="utf-8")
    _mark_executable(root_bootstrap_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            probe_csv_path,
            checks_csv_path,
            ssh_config_handoff_path,
            root_bootstrap_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR39CloudAutoLoginAdminBootstrapArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        ssh_config_handoff_path=ssh_config_handoff_path,
        root_bootstrap_path=root_bootstrap_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r39_cloud_auto_login_admin_bootstrap(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r39-cloud-auto-login-admin-bootstrap",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r38_path = reports_dir / "phase3bb_r38" / "cloud_scheduler_install_repair_handoff.json"
    r11 = _read_json(r11_path)
    r38 = _read_json(r38_path)
    target = _resolve_target(
        r11,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    probes = _build_remote_probes(target, timeout_seconds=per_probe_timeout_seconds)
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    local_state = _local_ssh_state(target)
    checks = _bootstrap_checks(r11=r11, r38=r38, parsed=parsed, local_state=local_state)
    decision = _bootstrap_decision(checks, parsed, local_state)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "access_bootstrap_handoff_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_config_modified_by_codex": False,
        "root_bootstrap_executed_by_codex": False,
        "sudoers_modified_by_codex": False,
        "code_sync_executed_by_codex": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R39-CLOUD-AUTO-LOGIN-ADMIN-BOOTSTRAP",
        "phase_version": PHASE3BB_R39_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_ACCESS_BOOTSTRAP_HANDOFF",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r38_artifact_path": str(r38_path),
        "r11_context_available": bool(r11),
        "r38_context_available": bool(r38),
        "cloud_target": _target_payload(target),
        "local_ssh_state": local_state,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "bootstrap_checks": checks,
        "bootstrap_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    return [
        RemoteProbe("ssh_batch_login", "hostname && whoami", timeout_seconds),
        RemoteProbe(
            "sudo_noninteractive",
            "sudo -n true >/dev/null 2>&1 && echo SUDO_N_OK || echo SUDO_N_BLOCKED",
            timeout_seconds,
        ),
        RemoteProbe(
            "admin_helper",
            (
                "test -x /usr/local/sbin/kalshi-scheduler-install-enable-no-start "
                "&& echo HELPER_PRESENT || echo HELPER_MISSING"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    ssh_stdout = _stdout(by_name.get("ssh_batch_login"))
    lines = [line.strip() for line in ssh_stdout.splitlines() if line.strip()]
    return {
        "ssh_batch_login_ok": bool(by_name.get("ssh_batch_login") and by_name["ssh_batch_login"].ok),
        "remote_hostname": lines[0] if lines else None,
        "remote_user": lines[1] if len(lines) > 1 else None,
        "sudo_noninteractive_ok": "SUDO_N_OK" in _stdout(by_name.get("sudo_noninteractive")),
        "admin_helper_present": "HELPER_PRESENT" in _stdout(by_name.get("admin_helper")),
    }


def _local_ssh_state(target: CloudBootstrapTarget) -> dict[str, Any]:
    config_path = Path.home() / ".ssh" / "config"
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    identity_path = Path(target.identity_file).expanduser()
    return {
        "ssh_config_path": str(config_path),
        "ssh_config_exists": config_path.exists(),
        "alias": SSH_ALIAS,
        "alias_present": f"Host {SSH_ALIAS}" in config_text,
        "identity_file": str(identity_path),
        "identity_file_exists": identity_path.exists(),
        "identity_file_mode_octal": oct(identity_path.stat().st_mode & 0o777)
        if identity_path.exists()
        else None,
    }


def _bootstrap_checks(
    *,
    r11: dict[str, Any],
    r38: dict[str, Any],
    parsed: dict[str, Any],
    local_state: dict[str, Any],
) -> list[dict[str, Any]]:
    r38_decision = r38.get("repair_decision") or {}
    return [
        _check("r11_cloud_context_present", bool(r11), "R11 cloud context exists."),
        _check("r38_repair_context_present", bool(r38), "R38 repair handoff exists."),
        _check(
            "r38_ready_or_not_needed",
            r38_decision.get("status")
            in {"REPAIR_HANDOFF_READY_NO_START", "REPAIR_NOT_NEEDED_READY_FOR_TIMER_START_HANDOFF"},
            f"R38 status is {r38_decision.get('status')}.",
        ),
        _check(
            "identity_file_exists",
            bool(local_state.get("identity_file_exists")),
            f"Identity file: {local_state.get('identity_file')}.",
        ),
        _check(
            "ssh_batch_login_current_target_ok",
            bool(parsed.get("ssh_batch_login_ok")),
            "Current SSH target accepts key-based batch login.",
        ),
        _check(
            "admin_helper_needed_or_present",
            True,
            (
                "Admin helper is already present."
                if parsed.get("admin_helper_present")
                else "Admin helper is missing and root bootstrap will install it."
            ),
        ),
    ]


def _bootstrap_decision(
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
    local_state: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        status = "BLOCKED_AUTO_LOGIN_ADMIN_BOOTSTRAP"
        reason = f"First failing check: {failed[0]['check']}."
        next_command = "Review reports/phase3bb_r39/bootstrap_checks.csv"
        next_step = "Phase 3BB-R39 - Resolve Access Bootstrap Preconditions"
    else:
        status = "AUTO_LOGIN_ADMIN_BOOTSTRAP_READY"
        reason = (
            "Key-based SSH works, a local SSH alias handoff was generated, and a root "
            "bootstrap script was generated to install a least-privilege sudo helper."
        )
        next_command = (
            f"{SSH_CONFIG_ENV_VAR}={SSH_CONFIG_TOKEN} "
            "bash reports/phase3bb_r39/operator_ssh_config_handoff.sh"
        )
        next_step = "Phase 3BB-R37 - Rerun Scheduler Install Verification After Bootstrap"
    return {
        "status": status,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "ssh_alias": SSH_ALIAS,
        "ssh_alias_present": bool(local_state.get("alias_present")),
        "identity_file_exists": bool(local_state.get("identity_file_exists")),
        "ssh_batch_login_ok": bool(parsed.get("ssh_batch_login_ok")),
        "sudo_noninteractive_ok": bool(parsed.get("sudo_noninteractive_ok")),
        "admin_helper_present": bool(parsed.get("admin_helper_present")),
        "codex_modified_ssh_config": False,
        "codex_modified_sudoers": False,
        "codex_started_scheduler": False,
        "operator_next_command": next_command,
        "next_codex_step": next_step,
    }


def _render_ssh_config_handoff(payload: dict[str, Any]) -> str:
    target = payload["cloud_target"]
    host = target["ssh_target"].split("@", 1)[-1]
    user = target["ssh_target"].split("@", 1)[0] if "@" in target["ssh_target"] else "kalshi"
    identity = target["identity_file"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TOKEN=${{{SSH_CONFIG_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(SSH_CONFIG_TOKEN)}",
        "CONFIG=\"$HOME/.ssh/config\"",
        "ROOT_HELPER_LOCAL=\"reports/phase3bb_r39/root_console_admin_bootstrap.sh\"",
        f"ROOT_HELPER_REMOTE={_shell_quote(ROOT_BOOTSTRAP_REMOTE_PATH)}",
        "",
        "echo '[phase3bb-r39] SSH auto-login handoff'",
        "echo '[phase3bb-r39] default mode is dry-run; no local ssh config changes occur'",
        "",
        "if [[ \"$TOKEN\" != \"$REQUIRED\" ]]; then",
        "  echo '[phase3bb-r39] dry-run:'",
        f"  echo '  add Host {SSH_ALIAS} to ~/.ssh/config'",
        f"  echo '  scp root bootstrap to {ROOT_BOOTSTRAP_REMOTE_PATH}'",
        f"  echo \"[phase3bb-r39] to execute: {SSH_CONFIG_ENV_VAR}=$REQUIRED bash $0\"",
        "  exit 0",
        "fi",
        "",
        "mkdir -p \"$HOME/.ssh\"",
        "chmod 700 \"$HOME/.ssh\"",
        "touch \"$CONFIG\"",
        "chmod 600 \"$CONFIG\"",
        f"if ! grep -q '^Host {SSH_ALIAS}$' \"$CONFIG\"; then",
        "  cp \"$CONFIG\" \"$CONFIG.phase3bb_r39.bak\"",
        "  cat >> \"$CONFIG\" <<'EOF'",
        "",
        f"Host {SSH_ALIAS}",
        f"  HostName {host}",
        f"  User {user}",
        f"  IdentityFile {identity}",
        "  IdentitiesOnly yes",
        "  BatchMode yes",
        "  ServerAliveInterval 30",
        "  ServerAliveCountMax 3",
        "EOF",
        "fi",
        "",
        f"ssh {SSH_ALIAS} 'echo SSH_ALIAS_OK && hostname && whoami'",
        f"scp \"$ROOT_HELPER_LOCAL\" {SSH_ALIAS}:\"$ROOT_HELPER_REMOTE\"",
        f"ssh {SSH_ALIAS} \"chmod 700 '$ROOT_HELPER_REMOTE' && ls -l '$ROOT_HELPER_REMOTE'\"",
        "",
        "echo '[phase3bb-r39] SSH alias is ready'",
        f"echo '[phase3bb-r39] future login: ssh {SSH_ALIAS}'",
        f"echo '[phase3bb-r39] root console next: bash {ROOT_BOOTSTRAP_REMOTE_PATH}'",
        "",
    ]
    return "\n".join(lines)


def _render_root_bootstrap(payload: dict[str, Any]) -> str:
    app = payload["cloud_target"]["app_path"].rstrip("/")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "SERVICE='kalshi-multicategory-refresh-scheduler.service'",
        "TIMER='kalshi-multicategory-refresh-scheduler.timer'",
        "RUNNER='kalshi-multicategory-refresh-runner.sh'",
        f"APP={_shell_quote(app)}",
        "HELPER='/usr/local/sbin/kalshi-scheduler-install-enable-no-start'",
        "SUDOERS='/etc/sudoers.d/90-kalshi-bot-scheduler'",
        "",
        "if [[ \"$(id -u)\" -ne 0 ]]; then",
        "  echo '[phase3bb-r39] run this script as root from the cloud console'",
        "  exit 2",
        "fi",
        "",
        "cat > \"$HELPER\" <<'EOF'",
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "SERVICE='kalshi-multicategory-refresh-scheduler.service'",
        "TIMER='kalshi-multicategory-refresh-scheduler.timer'",
        "RUNNER='kalshi-multicategory-refresh-runner.sh'",
        f"APP={_shell_quote(app)}",
        "test -f \"/tmp/${SERVICE}\"",
        "test -f \"/tmp/${TIMER}\"",
        "test -f \"/tmp/${RUNNER}\"",
        "install -D -m 0755 \"/tmp/${RUNNER}\" \"${APP}/scripts/${RUNNER}\"",
        "install -m 0644 \"/tmp/${SERVICE}\" \"/etc/systemd/system/${SERVICE}\"",
        "install -m 0644 \"/tmp/${TIMER}\" \"/etc/systemd/system/${TIMER}\"",
        "systemctl daemon-reload",
        "systemctl enable \"${TIMER}\"",
        "systemctl is-active \"${TIMER}\" || true",
        "systemctl is-active \"${SERVICE}\" || true",
        "EOF",
        "chmod 0750 \"$HELPER\"",
        "chown root:root \"$HELPER\"",
        "",
        "cat > \"$SUDOERS\" <<EOF",
        "Defaults:kalshi !requiretty",
        "kalshi ALL=(root) NOPASSWD: $HELPER",
        "EOF",
        "chmod 0440 \"$SUDOERS\"",
        "visudo -cf \"$SUDOERS\"",
        "",
        "echo '[phase3bb-r39] running helper once to install scheduler files and enable timer without starting'",
        "\"$HELPER\"",
        "echo '[phase3bb-r39] verifying noninteractive sudo helper'",
        "sudo -u kalshi sudo -n \"$HELPER\"",
        "",
        "echo '[phase3bb-r39] admin bootstrap complete'",
        "echo '[phase3bb-r39] do not start the timer until R37 verifies cleanly'",
        "",
    ]
    return "\n".join(lines)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R39 Cloud Auto Login Admin Bootstrap")
    decision = payload["bootstrap_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Access State",
            "",
            f"- SSH alias: `{decision['ssh_alias']}`",
            f"- Alias already present: `{decision['ssh_alias_present']}`",
            f"- Key exists: `{decision['identity_file_exists']}`",
            f"- Batch SSH works: `{decision['ssh_batch_login_ok']}`",
            f"- Noninteractive sudo already works: `{decision['sudo_noninteractive_ok']}`",
            f"- Admin helper present: `{decision['admin_helper_present']}`",
            "",
            "## What This Fixes",
            "",
            "- Future cloud login becomes `ssh kalshi-cloud`.",
            "- Future scheduler install can use a root-installed helper instead of prompting for sudo.",
            "- The helper installs/enables the scheduler timer without starting it.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R39 Auto Login/Admin Bootstrap Detail")
    decision = payload["bootstrap_decision"]
    lines.extend(
        [
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
    for row in payload["bootstrap_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Parsed Remote State", "", "```json"])
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.extend(["```", "", "## Local SSH State", "", "```json"])
    lines.append(json.dumps(payload["local_ssh_state"], indent=2, sort_keys=True))
    lines.extend(["```"])
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["bootstrap_decision"]["operator_next_command"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", "", command, ""])


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["bootstrap_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R39 Next Actions")
    lines.extend(
        [
            "",
            "## Step 1 - Local Auto Login",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "## Step 2 - Root Console Bootstrap",
            "",
            "Open the DigitalOcean/root console and run:",
            "",
            "```bash",
            f"bash {ROOT_BOOTSTRAP_REMOTE_PATH}",
            "```",
            "",
            "## Step 3 - Existing Code Sync Gate",
            "",
            "If R8 is still missing, run:",
            "",
            "```bash",
            "PHASE3BB_R38_CODE_SYNC=I_APPROVE_R38_CODE_SYNC bash reports/phase3bb_r38/operator_code_sync_handoff.sh",
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not start the scheduler timer yet.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


def _shell_quote(value: str) -> str:
    return shlex.quote(str(value))


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "ok",
        "exit_code",
        "duration_seconds",
        "timed_out",
        "stdout_excerpt",
        "stderr_excerpt",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        current = path.stat().st_mode
        path.chmod(current | 0o111)
    except OSError:
        pass
