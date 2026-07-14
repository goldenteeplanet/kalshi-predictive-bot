from __future__ import annotations

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
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R11_VERSION = "phase3bb_r11_codex_cloud_bridge_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r11")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SSH_ALIAS = "kalshi-cloud"
DEFAULT_CLOUD_HOST = "YOUR_DROPLET_IP"
DEFAULT_CLOUD_USER = "root"
DEFAULT_APP_PATH = "/opt/kalshi-predictive-bot"
DEFAULT_ENV_PATH = "/etc/kalshi-bot/kalshi-bot.env"
DEFAULT_DB_PATH = "/var/lib/kalshi-bot/kalshi_phase1.db"
DEFAULT_SERVICE_NAME = "kalshi-r5-watcher.service"
DEFAULT_IDENTITY_FILE = "~/.ssh/id_ed25519"


@dataclass(frozen=True)
class Phase3BBR11CodexCloudBridgeArtifacts:
    output_dir: Path
    executive_summary_path: Path
    bridge_markdown_path: Path
    operator_commands_path: Path
    smoke_test_path: Path
    context_json_path: Path
    next_actions_path: Path
    readme_for_codex_path: Path
    manifest_path: Path


def write_phase3bb_r11_codex_cloud_bridge_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    cloud_host: str = DEFAULT_CLOUD_HOST,
    cloud_user: str = DEFAULT_CLOUD_USER,
    ssh_alias: str = DEFAULT_SSH_ALIAS,
    app_path: str = DEFAULT_APP_PATH,
    env_path: str = DEFAULT_ENV_PATH,
    db_path: str = DEFAULT_DB_PATH,
    service_name: str = DEFAULT_SERVICE_NAME,
    identity_file: str = DEFAULT_IDENTITY_FILE,
) -> Phase3BBR11CodexCloudBridgeArtifacts:
    payload = build_phase3bb_r11_codex_cloud_bridge(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        cloud_host=cloud_host,
        cloud_user=cloud_user,
        ssh_alias=ssh_alias,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        identity_file=identity_file,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    bridge_markdown_path = output_dir / "codex_cloud_bridge.md"
    operator_commands_path = output_dir / "operator_connect_commands.sh"
    smoke_test_path = output_dir / "cloud_smoke_test.sh"
    context_json_path = output_dir / "codex_cloud_context.json"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    readme_for_codex_path = output_dir / "README_FOR_CODEX.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    bridge_markdown_path.write_text(_render_bridge_markdown(payload), encoding="utf-8")
    operator_commands_path.write_text(_render_operator_connect_commands(payload), encoding="utf-8")
    smoke_test_path.write_text(_render_cloud_smoke_test(payload), encoding="utf-8")
    context_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    readme_for_codex_path.write_text(_render_readme_for_codex(payload), encoding="utf-8")
    _mark_executable(operator_commands_path)
    _mark_executable(smoke_test_path)
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            bridge_markdown_path,
            operator_commands_path,
            smoke_test_path,
            context_json_path,
            next_actions_path,
            readme_for_codex_path,
        ],
    )
    return Phase3BBR11CodexCloudBridgeArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        bridge_markdown_path=bridge_markdown_path,
        operator_commands_path=operator_commands_path,
        smoke_test_path=smoke_test_path,
        context_json_path=context_json_path,
        next_actions_path=next_actions_path,
        readme_for_codex_path=readme_for_codex_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r11_codex_cloud_bridge(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    cloud_host: str = DEFAULT_CLOUD_HOST,
    cloud_user: str = DEFAULT_CLOUD_USER,
    ssh_alias: str = DEFAULT_SSH_ALIAS,
    app_path: str = DEFAULT_APP_PATH,
    env_path: str = DEFAULT_ENV_PATH,
    db_path: str = DEFAULT_DB_PATH,
    service_name: str = DEFAULT_SERVICE_NAME,
    identity_file: str = DEFAULT_IDENTITY_FILE,
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
        "command": "kalshi-bot phase3bb-r11-codex-cloud-bridge",
        "argv": command_args or [],
    }
    r10_decision = _read_json(reports_dir / "phase3bb_r10" / "cloud_readiness_decision.json")
    ssh_profile = {
        "alias": ssh_alias,
        "host": cloud_host,
        "user": cloud_user,
        "identity_file": identity_file,
        "server_alive_interval": 30,
        "placeholder_host": cloud_host == DEFAULT_CLOUD_HOST,
    }
    remote_paths = {
        "app_path": app_path,
        "env_path": env_path,
        "db_path": db_path,
        "reports_path": f"{app_path.rstrip('/')}/reports",
        "kalshi_bot_path": f"{app_path.rstrip('/')}/.venv/bin/kalshi-bot",
    }
    commands = _bridge_commands(
        ssh_alias=ssh_alias,
        cloud_host=cloud_host,
        cloud_user=cloud_user,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        identity_file=identity_file,
    )
    validation_checks = [
        "SSH alias resolves and accepts the configured key.",
        "Remote repo path exists.",
        "Remote .venv/bin/kalshi-bot exists and is executable.",
        "Remote env file exists but is not printed or copied into reports.",
        "Remote DB path exists.",
        "Remote phase3ba-status runs in PAPER/READ-ONLY mode.",
        "Remote reports can be mirrored with env/private files excluded.",
    ]
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "no_deploy": True,
        "ssh_commands_written_only": True,
        "remote_commands_executed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    next_operator_command = (
        f"kalshi-bot phase3bb-r11-codex-cloud-bridge --output-dir {output_dir} "
        f"--reports-dir {reports_dir} --cloud-host <your_droplet_ip>"
    )
    next_smoke_command = f"bash {output_dir / 'cloud_smoke_test.sh'} {ssh_alias}"
    return {
        **metadata,
        "phase": "3BB-R11-CODEX-CLOUD-BRIDGE",
        "phase_version": PHASE3BB_R11_VERSION,
        "mode": "PAPER_READ_ONLY_NO_DEPLOY_CLOUD_BRIDGE",
        "reports_dir": str(reports_dir),
        "r10_decision_summary": _summarize_r10_decision(r10_decision),
        "ssh_profile": ssh_profile,
        "remote_paths": remote_paths,
        "service_name": service_name,
        "bridge_commands": commands,
        "validation_checks": validation_checks,
        "codex_connection_contract": {
            "operate_method": "ssh_alias_or_pasted_smoke_output",
            "safe_default": "read_only_status_and_report_mirroring",
            "forbidden": [
                "copy env file contents into Codex",
                "deploy from this phase",
                "start duplicate R5 watchers",
                "create paper trades",
                "submit live/demo orders",
            ],
        },
        "next_operator_command": next_operator_command,
        "next_smoke_command": next_smoke_command,
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _summarize_r10_decision(r10_decision: dict[str, Any]) -> dict[str, Any]:
    decision = r10_decision.get("decision") or {}
    cost = r10_decision.get("cost_plan") or {}
    deployment = r10_decision.get("deployment_plan") or {}
    return {
        "available": bool(r10_decision),
        "status": decision.get("status", "UNKNOWN"),
        "buy_compute_now": decision.get("buy_compute_now"),
        "recommendation": decision.get("recommendation"),
        "recommended_architecture": decision.get("recommended_architecture"),
        "monthly_budget_usd": cost.get("monthly_budget_usd"),
        "budget_ceiling_usd": cost.get("budget_ceiling_usd"),
        "spec": cost.get("spec"),
        "deploy_now": deployment.get("deploy_now", False),
    }


def _bridge_commands(
    *,
    ssh_alias: str,
    cloud_host: str,
    cloud_user: str,
    app_path: str,
    env_path: str,
    db_path: str,
    identity_file: str,
) -> dict[str, str]:
    report_mirror_path = "./reports/cloud_mirror/"
    remote_status = (
        f"cd {app_path} && "
        f"set -a && . {env_path} && set +a && "
        ".venv/bin/kalshi-bot phase3ba-status "
        "--output-dir reports/phase3ba_status --reports-dir reports"
    )
    remote_smoke = (
        "hostname && whoami && "
        f"test -d {app_path} && "
        f"test -x {app_path.rstrip('/')}/.venv/bin/kalshi-bot && "
        f"test -f {env_path} && "
        f"test -f {db_path}"
    )
    return {
        "ssh_config_example": (
            "Host "
            f"{ssh_alias}\n"
            f"  HostName {cloud_host}\n"
            f"  User {cloud_user}\n"
            f"  IdentityFile {identity_file}\n"
            "  IdentitiesOnly yes\n"
            "  ServerAliveInterval 30"
        ),
        "direct_smoke_test": (
            f"bash reports/phase3bb_r11/cloud_smoke_test.sh {cloud_user}@{cloud_host}"
        ),
        "smoke_test": f"ssh {ssh_alias!s} '{remote_smoke}'",
        "read_only_status": f"ssh {ssh_alias!s} '{remote_status}'",
        "mirror_reports_only": (
            "rsync -avz --exclude '*.env' --exclude '*private*' "
            f"--exclude '*secret*' {ssh_alias}:{app_path.rstrip('/')}/reports/ "
            f"{report_mirror_path}"
        ),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R11 Codex Cloud Bridge")
    r10 = payload["r10_decision_summary"]
    ssh = payload["ssh_profile"]
    paths = payload["remote_paths"]
    lines.extend(
        [
            "",
            "## Purpose",
            "",
            "- Generate the safe connection pack Codex needs to inspect a cloud host.",
            "- No SSH command was executed by this phase.",
            "- No deployment, service start, DB write, paper trade, or exchange order occurred.",
            "",
            "## Cloud Decision Context",
            "",
            f"- R10 available: `{r10['available']}`",
            f"- R10 status: `{r10['status']}`",
            f"- Buy compute now: `{r10['buy_compute_now']}`",
            f"- Recommendation: `{r10['recommendation']}`",
            f"- Spec: `{r10['spec']}`",
            "",
            "## Connection Target",
            "",
            f"- SSH alias: `{ssh['alias']}`",
            f"- Cloud host: `{ssh['host']}`",
            f"- Placeholder host still set: `{ssh['placeholder_host']}`",
            f"- Remote app path: `{paths['app_path']}`",
            f"- Remote env path: `{paths['env_path']}`",
            f"- Remote DB path: `{paths['db_path']}`",
            "",
            "## Exact Next Command",
            "",
            f"```bash\n{payload['next_operator_command']}\n```",
            "",
            "After SSH is configured, run:",
            "",
            f"```bash\n{payload['next_smoke_command']}\n```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_bridge_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R11 Codex Cloud Bridge Detail")
    commands = payload["bridge_commands"]
    lines.extend(
        [
            "",
            "## SSH Config Example",
            "",
            "Add this manually to your local `~/.ssh/config` after replacing the host.",
            "",
            f"```sshconfig\n{commands['ssh_config_example']}\n```",
            "",
            "## Smoke Test",
            "",
            "This checks presence only. It does not print the env file.",
            "",
            f"```bash\n{commands['smoke_test']}\n```",
            "",
            "## Read-Only Remote Status",
            "",
            f"```bash\n{commands['read_only_status']}\n```",
            "",
            "## Mirror Reports Only",
            "",
            f"```bash\n{commands['mirror_reports_only']}\n```",
            "",
            "## Validation Checks",
            "",
        ]
    )
    for check in payload["validation_checks"]:
        lines.append(f"- [ ] {check}")
    lines.extend(
        [
            "",
            "## Do Not Run In This Phase",
            "",
            "- Do not deploy or install services.",
            "- Do not copy env contents into Codex or reports.",
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_connect_commands(payload: dict[str, Any]) -> str:
    ssh = payload["ssh_profile"]
    commands = payload["bridge_commands"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "# Phase 3BB-R11 Codex cloud bridge helper.",
        "# This file prints the safe commands only; it does not run SSH automatically.",
        "# Replace YOUR_DROPLET_IP in ~/.ssh/config before running the smoke test.",
        "",
        "cat <<'EOF'",
        "# ~/.ssh/config",
        commands["ssh_config_example"],
        "EOF",
        "",
        "cat <<'EOF'",
            "# Safe smoke test",
            commands["direct_smoke_test"],
            commands["smoke_test"],
            "",
        "# Read-only bot status",
        commands["read_only_status"],
        "",
        "# Reports-only mirror",
        commands["mirror_reports_only"],
        "EOF",
        "",
        f"echo 'SSH alias: {ssh['alias']}'",
    ]
    return "\n".join(lines) + "\n"


def _render_cloud_smoke_test(payload: dict[str, Any]) -> str:
    paths = payload["remote_paths"]
    ssh_profile = payload["ssh_profile"]
    direct_target = f"{ssh_profile['user']}@{ssh_profile['host']}"
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"SSH_TARGET=\"${{1:-{ssh_profile['alias']}}}\"",
            f"DIRECT_TARGET=\"${{DIRECT_TARGET:-{direct_target}}}\"",
            f"IDENTITY_FILE=\"${{IDENTITY_FILE:-{ssh_profile['identity_file']}}}\"",
            f"APP_PATH=\"${{APP_PATH:-{paths['app_path']}}}\"",
            f"ENV_PATH=\"${{ENV_PATH:-{paths['env_path']}}}\"",
            f"DB_PATH=\"${{DB_PATH:-{paths['db_path']}}}\"",
            "",
            "SSH_OPTS=(",
            "  -o BatchMode=yes",
            "  -o ConnectTimeout=15",
            "  -o IdentitiesOnly=yes",
            "  -i \"$IDENTITY_FILE\"",
            ")",
            "run_remote() {",
            "  ssh \"${SSH_OPTS[@]}\" \"$SSH_TARGET\" \"$@\"",
            "}",
            "",
            "echo \"[phase3bb-r11] checking $SSH_TARGET\"",
            "if ! run_remote 'hostname && whoami'; then",
            "  if [[ \"$SSH_TARGET\" == \"kalshi-cloud\" && "
            "\"$DIRECT_TARGET\" != *@YOUR_DROPLET_IP ]]; then",
            "    echo \"[phase3bb-r11] SSH alias failed; retrying $DIRECT_TARGET\"",
            "    SSH_TARGET=\"$DIRECT_TARGET\"",
            "    run_remote 'hostname && whoami'",
            "  else",
            "    exit 1",
            "  fi",
            "fi",
            "run_remote \"test -d '$APP_PATH'\"",
            "run_remote \"test -x '$APP_PATH/.venv/bin/kalshi-bot'\"",
            "run_remote \"test -f '$ENV_PATH'\"",
            "run_remote \"test -f '$DB_PATH'\"",
            "run_remote \"cd '$APP_PATH' && "
            "set -a && . '$ENV_PATH' && set +a && "
            ".venv/bin/kalshi-bot phase3ba-status "
            "--output-dir reports/phase3ba_status --reports-dir reports\"",
            "echo '[phase3bb-r11] smoke test complete; env contents were not printed'",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R11 Next Actions")
    lines.extend(
        [
            "",
            "## Operator Next Step",
            "",
            "1. Replace the placeholder cloud host in the generated command or SSH config.",
            "2. Run the bridge command again with the real host so reports capture the target.",
            "3. Run the smoke test after SSH is configured.",
            "4. Paste the smoke-test result into Codex before any deploy or service start.",
            "",
            "## Commands",
            "",
            f"```bash\n{payload['next_operator_command']}\n```",
            "",
            f"```bash\n{payload['next_smoke_command']}\n```",
            "",
            "## Stop Conditions",
            "",
            "- Stop if SSH cannot connect.",
            "- Stop if the env file or DB file is missing.",
            "- Stop if phase3ba-status fails.",
            "- Stop if more than one R5 watcher is running.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_readme_for_codex(payload: dict[str, Any]) -> str:
    paths = payload["remote_paths"]
    lines = _metadata_lines(payload, "# README For Codex Cloud Task")
    lines.extend(
        [
            "",
            "## Context",
            "",
            "Use this file when opening a Codex task for the cloud host.",
            "",
            f"- Remote repo: `{paths['app_path']}`",
            f"- Remote reports: `{paths['reports_path']}`",
            f"- Remote DB: `{paths['db_path']}`",
            "- Remote env exists at the configured path, but its contents must not be pasted.",
            "",
            "## First Remote Commands",
            "",
            "```bash",
            "kalshi-bot db-writer-monitor --json",
            "kalshi-bot phase3ba-status --output-dir reports/phase3ba_status --reports-dir reports",
            "kalshi-bot phase3bb-r1-operator-scheduler "
            "--output-dir reports/phase3bb_r1 --reports-dir reports",
            "```",
            "",
            "## Guardrails",
            "",
            "- PAPER/READ-ONLY unless a later phase explicitly authorizes a local write.",
            "- No paper trades.",
            "- No live/demo orders.",
            "- One R5 watcher max.",
            "- Run db-writer-monitor before any write-capable local command.",
        ]
    )
    return "\n".join(lines) + "\n"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
