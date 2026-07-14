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

PHASE3BB_R14_VERSION = "phase3bb_r14_cloud_service_plan_draft_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r14")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SERVICE_NAME = "kalshi-r5-watcher.service"
DEFAULT_GUARD_SCRIPT_PATH = "/opt/kalshi-predictive-bot/scripts/cloud/kalshi-r5-start-guard.sh"


@dataclass(frozen=True)
class Phase3BBR14CloudServicePlanArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    service_draft_path: Path
    guard_script_draft_path: Path
    install_checklist_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r14_cloud_service_plan_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    adopt_existing_r5: bool = False,
    service_name: str = DEFAULT_SERVICE_NAME,
    guard_script_path: str = DEFAULT_GUARD_SCRIPT_PATH,
) -> Phase3BBR14CloudServicePlanArtifacts:
    payload = build_phase3bb_r14_cloud_service_plan(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        adopt_existing_r5=adopt_existing_r5,
        service_name=service_name,
        guard_script_path=guard_script_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_service_plan.md"
    json_path = output_dir / "cloud_service_plan.json"
    service_draft_path = output_dir / f"{service_name}.draft"
    guard_script_draft_path = output_dir / "kalshi-r5-start-guard.sh.draft"
    install_checklist_path = output_dir / "install_review_checklist.md"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    service_draft_path.write_text(_render_service_draft(payload), encoding="utf-8")
    guard_script_draft_path.write_text(_render_guard_script_draft(payload), encoding="utf-8")
    install_checklist_path.write_text(_render_install_checklist(payload), encoding="utf-8")
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            service_draft_path,
            guard_script_draft_path,
            install_checklist_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR14CloudServicePlanArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        service_draft_path=service_draft_path,
        guard_script_draft_path=guard_script_draft_path,
        install_checklist_path=install_checklist_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r14_cloud_service_plan(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    adopt_existing_r5: bool = False,
    service_name: str = DEFAULT_SERVICE_NAME,
    guard_script_path: str = DEFAULT_GUARD_SCRIPT_PATH,
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
        "command": "kalshi-bot phase3bb-r14-cloud-service-plan",
        "argv": command_args or [],
    }
    r13_context = _read_json(reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json")
    r12_context = _read_json(
        reports_dir / "phase3bb_r12" / "cloud_bootstrap_verification.json"
    )
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    adoption = r13_context.get("adoption_decision") or {}
    target = _target_from_contexts(r13_context, r11_context)
    active_command = _active_writer_command(r12_context)
    service_plan = _service_plan(
        adoption=adoption,
        target=target,
        active_command=active_command,
        adopt_existing_r5=adopt_existing_r5,
        service_name=service_name,
        guard_script_path=guard_script_path,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
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
        "phase": "3BB-R14-CLOUD-SERVICE-PLAN-DRAFT",
        "phase_version": PHASE3BB_R14_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SERVICE_PLAN_DRAFT",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "r12_context_available": bool(r12_context),
        "r13_context_available": bool(r13_context),
        "cloud_target": target,
        "adoption_decision": adoption,
        "active_r5_command": active_command,
        "service_plan": service_plan,
        "next_operator_command": service_plan["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _target_from_contexts(
    r13_context: dict[str, Any],
    r11_context: dict[str, Any],
) -> dict[str, Any]:
    target = dict(r13_context.get("cloud_target") or {})
    if target:
        return target
    ssh_profile = r11_context.get("ssh_profile") or {}
    paths = r11_context.get("remote_paths") or {}
    user = ssh_profile.get("user") or "kalshi"
    host = ssh_profile.get("host") or "159.65.35.72"
    app_path = paths.get("app_path") or "/opt/kalshi-predictive-bot"
    return {
        "ssh_target": f"{user}@{host}",
        "identity_file": ssh_profile.get("identity_file") or "~/.ssh/id_ed25519",
        "app_path": app_path,
        "env_path": paths.get("env_path") or "/etc/kalshi-bot/kalshi-bot.env",
        "db_path": paths.get("db_path") or "/var/lib/kalshi-bot/kalshi_phase1.db",
        "reports_path": paths.get("reports_path") or f"{str(app_path).rstrip('/')}/reports",
    }


def _active_writer_command(r12_context: dict[str, Any]) -> str:
    parsed = r12_context.get("parsed_remote_state") or {}
    phase3ba_status = parsed.get("phase3ba_status") or {}
    summary = phase3ba_status.get("summary") or {}
    command = str(summary.get("active_writer_command") or "").strip()
    if command:
        return command
    return (
        "/opt/kalshi-predictive-bot/.venv/bin/python -m kalshi_predictor.cli "
        "phase3bc-r5-crypto-freshness-watch --output-dir reports/phase3bc_r5 "
        "--phase3bc-output-dir reports/phase3bc --phase3bc-r3-output-dir reports/phase3bc_r3 "
        "--phase3bc-r4-output-dir reports/phase3bc_r4 --phase3bc-r7-output-dir "
        "reports/phase3bc_r7 --symbols BTC,ETH,SOL,XRP,DOGE --crypto-series-tickers "
        "KXBTC,KXETH,KXSOLE,KXXRP,KXDOGE --source coinbase --market-limit 150 "
        "--market-max-pages 1 --crypto-market-scan-limit 2500 --crypto-link-limit 500 "
        "--forecast-limit 1000 --opportunity-limit 500 --phase3bc-limit 1000 "
        "--cadence-minutes 15 --freshness-minutes 15 --max-preflight 10 "
        "--ranking-repair-limit 500 --cycles 32 --interval-minutes 15 "
        "--near-money-per-symbol-limit 40 --near-money-window-limit 20 "
        "--snapshot-fetch-concurrency 2 --refresh-open-markets --external-crypto-ingest "
        "--diagnose-snapshots --forecast-current-windows-only --skip-opportunity-report "
        "--risk-preflight --ranking-repair --near-money-only"
    )


def _service_plan(
    *,
    adoption: dict[str, Any],
    target: dict[str, Any],
    active_command: str,
    adopt_existing_r5: bool,
    service_name: str,
    guard_script_path: str,
) -> dict[str, Any]:
    recommendation = str(adoption.get("recommendation") or "UNKNOWN")
    current_pid = adoption.get("current_r5_pid")
    guard_status = adoption.get("guard_status")
    guard_should_stop = bool(adoption.get("guard_should_stop"))
    ready = adopt_existing_r5 and recommendation == "ADOPT_EXISTING_R5" and not guard_should_stop
    if ready:
        status = "DRAFT_READY_FOR_REVIEW"
        reason = (
            "R13 recommended ADOPT_EXISTING_R5. Draft files are ready for review, "
            "but no install/start is allowed in R14."
        )
    else:
        status = "BLOCKED_BY_ADOPTION_GATE"
        reason = (
            "R13 did not provide a clean ADOPT_EXISTING_R5 gate, or --adopt-existing-r5 "
            "was not set."
        )
    return {
        "status": status,
        "ready_for_review": ready,
        "adopt_existing_r5_requested": adopt_existing_r5,
        "install_allowed_now": False,
        "start_allowed_now": False,
        "enable_allowed_now": False,
        "service_name": service_name,
        "guard_script_path": guard_script_path,
        "existing_r5_pid": current_pid,
        "r13_recommendation": recommendation,
        "r5_guard_status": guard_status,
        "r5_guard_should_stop": guard_should_stop,
        "primary_reason": reason,
        "operator_next_command": (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        ),
        "next_codex_step": (
            "Phase 3BB-R15 - Cloud Service Install Review / No-start Dry Run"
        ),
        "active_command": active_command,
        "remote_app_path": target.get("app_path"),
        "remote_env_path": target.get("env_path"),
        "remote_reports_path": target.get("reports_path"),
        "remote_db_path": target.get("db_path"),
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R14 Cloud Service Plan Draft")
    plan = payload["service_plan"]
    adoption = payload["adoption_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{plan['status']}`",
            f"- Ready for review: `{plan['ready_for_review']}`",
            f"- Install allowed now: `{plan['install_allowed_now']}`",
            f"- Start allowed now: `{plan['start_allowed_now']}`",
            f"- Existing R5 PID: `{plan['existing_r5_pid']}`",
            f"- R13 recommendation: `{plan['r13_recommendation']}`",
            f"- Guard status: `{plan['r5_guard_status']}`",
            f"- Guard should stop: `{plan['r5_guard_should_stop']}`",
            f"- Reason: {plan['primary_reason']}",
            "",
            "## Adoption Context",
            "",
            f"- Current R5 PID: `{adoption.get('current_r5_pid')}`",
            f"- Writer matches R5: `{adoption.get('writer_matches_r5')}`",
            f"- Watch state: `{adoption.get('watch_state')}`",
            "",
            "## Draft Artifacts",
            "",
            f"- Service draft: `{plan['service_name']}.draft`",
            "- Guard script draft: `kalshi-r5-start-guard.sh.draft`",
            "- Install checklist: `install_review_checklist.md`",
            "",
            "## Safety",
            "",
            "- No systemd file was installed.",
            "- No service was enabled or started.",
            "- Existing R5 was not stopped.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Command",
            "",
            f"```bash\n{plan['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {plan['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R14 Cloud Service Plan Detail")
    plan = payload["service_plan"]
    lines.extend(
        [
            "",
            "## Strategy",
            "",
            "Adopt the current healthy R5 watcher by leaving it alone. The service draft "
            "is prepared for a later reviewed transition. It refuses duplicate starts "
            "through an `ExecStartPre` guard script.",
            "",
            "## Plan",
            "",
            f"- Status: `{plan['status']}`",
            f"- Existing R5 PID: `{plan['existing_r5_pid']}`",
            f"- Service name: `{plan['service_name']}`",
            f"- Guard script path: `{plan['guard_script_path']}`",
            f"- Remote app path: `{plan['remote_app_path']}`",
            f"- Remote env path: `{plan['remote_env_path']}`",
            f"- Remote DB path: `{plan['remote_db_path']}`",
            "",
            "## Active R5 Command",
            "",
            "```bash",
            plan["active_command"],
            "```",
            "",
            "## Review Gates Before Any Install",
            "",
            "- R13 still recommends `ADOPT_EXISTING_R5`.",
            "- `phase3bc-r5-status` shows one R5 process only.",
            "- Guard `should_stop` is false.",
            "- `db-writer-monitor --json` is clear or the active writer is the adopted R5.",
            "- Operator explicitly approves the install/start phase.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_service_draft(payload: dict[str, Any]) -> str:
    plan = payload["service_plan"]
    return "\n".join(
        [
            "[Unit]",
            "Description=Kalshi Bot R5 crypto freshness watcher (paper-only)",
            "Wants=network-online.target",
            "After=network-online.target",
            f"ConditionPathExists={plan['remote_env_path']}",
            f"ConditionPathExists={plan['remote_db_path']}",
            "StartLimitIntervalSec=900",
            "StartLimitBurst=2",
            "",
            "[Service]",
            "Type=simple",
            "User=kalshi",
            f"WorkingDirectory={plan['remote_app_path']}",
            f"EnvironmentFile={plan['remote_env_path']}",
            "Environment=PYTHONUNBUFFERED=1",
            f"ExecStartPre={plan['guard_script_path']}",
            f"ExecStart={plan['active_command']}",
            "Restart=always",
            "RestartSec=300",
            "TimeoutStopSec=60",
            "KillSignal=SIGTERM",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            f"ReadWritePaths={plan['remote_reports_path']} /var/lib/kalshi-bot",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _render_guard_script_draft(payload: dict[str, Any]) -> str:
    plan = payload["service_plan"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"APP_PATH={_shell_quote(str(plan['remote_app_path']))}",
            f"ENV_PATH={_shell_quote(str(plan['remote_env_path']))}",
            "",
            "cd \"$APP_PATH\"",
            "existing_pids=$(pgrep -f 'phase3bc-r5-crypto-freshness-watch' || true)",
            "if [[ -n \"$existing_pids\" ]]; then",
            "  echo \"Refusing duplicate R5 start; existing pid(s): $existing_pids\" >&2",
            "  exit 75",
            "fi",
            "",
            "set -a",
            ". \"$ENV_PATH\"",
            "set +a",
            "",
            ".venv/bin/kalshi-bot db-writer-monitor --json > /tmp/kalshi-r5-writer-guard.json",
            "if grep -q '\"safe_to_start_write\": false' /tmp/kalshi-r5-writer-guard.json; then",
            "  echo 'Refusing R5 start because db-writer-monitor is not clear.' >&2",
            "  exit 75",
            "fi",
            "",
            "echo 'R5 start guard passed.'",
            "",
        ]
    )


def _render_install_checklist(payload: dict[str, Any]) -> str:
    plan = payload["service_plan"]
    lines = _metadata_lines(payload, "# Phase 3BB-R14 Install Review Checklist")
    lines.extend(
        [
            "",
            "## R14 Is Draft Only",
            "",
            "- [ ] Confirm R13 still recommends `ADOPT_EXISTING_R5`.",
            "- [ ] Confirm existing R5 PID has naturally exited, or approve a later handoff phase.",
            "- [ ] Confirm the guard script path and service file path.",
            "- [ ] Review the exact `ExecStart` command.",
            "- [ ] Confirm no duplicate R5 watcher exists.",
            "- [ ] Confirm `db-writer-monitor --json` is clear before install/start.",
            "- [ ] Confirm no paper/live/demo trading command is present.",
            "",
            "## Deferred Paths",
            "",
            f"- Service name: `{plan['service_name']}`",
            f"- Guard script path: `{plan['guard_script_path']}`",
            "",
            "Do not copy, install, enable, or start these files in R14.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    plan = payload["service_plan"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# R14 is draft-only. Re-run R13 before any install/start phase.",
            plan["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R14 Next Actions")
    plan = payload["service_plan"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{plan['status']}`",
            f"- Reason: {plan['primary_reason']}",
            "",
            "```bash",
            plan["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {plan['next_codex_step']}",
            "",
            "## Do Not Run Yet",
            "",
            "- Do not install the service draft.",
            "- Do not enable or start the service.",
            "- Do not stop the existing R5 watcher.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
