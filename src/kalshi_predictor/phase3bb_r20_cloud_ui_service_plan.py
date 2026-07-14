from __future__ import annotations

import csv
import json
import shlex
import time
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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R20_VERSION = "phase3bb_r20_cloud_ui_service_plan_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r20")
DEFAULT_UI_SERVICE_NAME = "kalshi-ui.service"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8080


@dataclass(frozen=True)
class Phase3BBR20CloudUiServicePlanArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    service_draft_path: Path
    nginx_draft_path: Path
    install_checklist_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r20_cloud_ui_service_plan_report(
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
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_host: str = DEFAULT_UI_HOST,
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR20CloudUiServicePlanArtifacts:
    payload = build_phase3bb_r20_cloud_ui_service_plan(
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
        ui_service_name=ui_service_name,
        ui_host=ui_host,
        ui_port=ui_port,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_service_plan.md"
    json_path = output_dir / "cloud_ui_service_plan.json"
    service_draft_path = output_dir / f"{ui_service_name}.draft"
    nginx_draft_path = output_dir / "kalshi-ui.nginx.draft"
    install_checklist_path = output_dir / "ui_install_review_checklist.md"
    probe_csv_path = output_dir / "remote_ui_probe_results.csv"
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
    nginx_draft_path.write_text(_render_nginx_draft(payload), encoding="utf-8")
    install_checklist_path.write_text(_render_install_checklist(payload), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_ui_probe_results"])
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
            nginx_draft_path,
            install_checklist_path,
            probe_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR20CloudUiServicePlanArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        service_draft_path=service_draft_path,
        nginx_draft_path=nginx_draft_path,
        install_checklist_path=install_checklist_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r20_cloud_ui_service_plan(
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
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_host: str = DEFAULT_UI_HOST,
    ui_port: int = DEFAULT_UI_PORT,
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
        "command": "kalshi-bot phase3bb-r20-cloud-ui-service-plan",
        "argv": command_args or [],
    }
    runner = probe_runner or _run_ssh_probe
    r18 = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
        session,
        output_dir=output_dir / "r18_preflight",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r18-cloud-scheduler-runtime-cutover"],
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=runner,
    )
    target = dict(r18.get("cloud_target") or {})
    probes = _build_ui_probe_commands(
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    results = [runner(probe, _target_from_payload(target)) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    ui_state = _parse_ui_probe_results(results, ui_service_name=ui_service_name)
    service_plan = _service_plan(
        r18=r18,
        target=target,
        ui_state=ui_state,
        ui_service_name=ui_service_name,
        ui_host=ui_host,
        ui_port=ui_port,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
        "no_service_enable": True,
        "no_service_start": True,
        "service_files_written_to_system": False,
        "remote_commands_executed": len(results),
        "remote_db_writes_performed": 0,
        "remote_report_writes_only": True,
        "systemctl_read_only_commands_executed": 3,
        "systemctl_mutating_commands_executed": 0,
        "ssh_commands_executed": len(results),
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
        "phase": "3BB-R20-CLOUD-UI-SERVICE-PLAN",
        "phase_version": PHASE3BB_R20_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_SERVICE_PLAN",
        "reports_dir": str(reports_dir),
        "r18_preflight": r18,
        "cloud_target": target,
        "ui_service_name": ui_service_name,
        "ui_bind_host": ui_host,
        "ui_bind_port": ui_port,
        "remote_ui_probe_duration_seconds": duration,
        "remote_ui_probe_results": [_result_payload(result) for result in results],
        "parsed_ui_state": ui_state,
        "ui_service_plan": service_plan,
        "next_operator_command": service_plan["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_ui_probe_commands(
    *,
    ui_service_name: str,
    ui_port: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    service = shlex.quote(ui_service_name)
    return [
        RemoteProbe(
            "ui_systemd_unit",
            (
                f"systemctl show {service} --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "ui_systemd_enabled",
            f"systemctl is-enabled {service} || true",
            timeout_seconds,
        ),
        RemoteProbe("ui_systemd_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe(
            "ui_processes",
            (
                "ps -eo pid=,args= | awk "
                "'(/kalshi-bot ui/ || /uvicorn .*kalshi_predictor.ui.app/) "
                "&& !/awk / {print}' || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "ui_listeners",
            (
                "ss -ltnp 2>/dev/null | "
                f"awk '$4 ~ /:({ui_port}|80|443)$/ {{print}}' || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "nginx_state",
            (
                "if command -v nginx >/dev/null 2>&1; then "
                "echo nginx_present; systemctl is-active nginx || true; "
                "else echo nginx_missing; fi"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "local_ui_http",
            (
                f"curl -fsS -m 5 http://127.0.0.1:{ui_port}/ >/tmp/phase3bb_r20_ui.html "
                "&& echo HTTP_OK || echo HTTP_NOT_READY"
            ),
            timeout_seconds,
        ),
    ]


def _parse_ui_probe_results(
    results: list[RemoteProbeResult],
    *,
    ui_service_name: str,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    systemd = _parse_systemd_show(_stdout(by_name.get("ui_systemd_unit")))
    enabled = _first_line(_stdout(by_name.get("ui_systemd_enabled")))
    active = _first_line(_stdout(by_name.get("ui_systemd_active")))
    process_text = _stdout(by_name.get("ui_processes"))
    listener_text = _stdout(by_name.get("ui_listeners"))
    nginx_text = _stdout(by_name.get("nginx_state"))
    http_text = _stdout(by_name.get("local_ui_http"))
    pids = _pids_from_process_output(process_text)
    service_exec_main_pid = _to_int(systemd.get("ExecMainPID"))
    service_active = active or systemd.get("ActiveState")
    return {
        "ui_service_name": ui_service_name,
        "systemd_unit": systemd,
        "service_loaded": systemd.get("LoadState") == "loaded",
        "service_enabled_state": enabled or systemd.get("UnitFileState"),
        "service_enabled": (enabled or systemd.get("UnitFileState")) == "enabled",
        "service_active_state": service_active,
        "service_started": service_active == "active" or bool(service_exec_main_pid),
        "service_exec_main_pid": service_exec_main_pid,
        "ui_process_pids": pids,
        "ui_duplicate_process": len(pids) > 1,
        "ui_process_text": process_text.strip(),
        "listener_text": listener_text.strip(),
        "ui_port_listening": _listener_has_port(listener_text, 8080),
        "public_http_listening": _listener_has_port(listener_text, 80),
        "public_https_listening": _listener_has_port(listener_text, 443),
        "nginx_present": "nginx_present" in nginx_text,
        "nginx_active": "active" in nginx_text.splitlines()[1:],
        "local_ui_http_ok": "HTTP_OK" in http_text,
    }


def _listener_has_port(text: str, port: int) -> bool:
    expected = str(port)
    for line in text.splitlines():
        for token in line.split():
            if ":" not in token:
                continue
            if token.rsplit(":", 1)[-1] == expected:
                return True
    return False


def _service_plan(
    *,
    r18: dict[str, Any],
    target: dict[str, Any],
    ui_state: dict[str, Any],
    ui_service_name: str,
    ui_host: str,
    ui_port: int,
) -> dict[str, Any]:
    r18_decision = r18.get("runtime_cutover_decision") or {}
    r5_ready = r18_decision.get("status") == "SYSTEMD_OWNS_R5"
    duplicate_ui = bool(ui_state.get("ui_duplicate_process"))
    if not r5_ready:
        status = "BLOCKED_R5_NOT_SYSTEMD_OWNED"
        ready = False
        reason = (
            f"R18 status is {r18_decision.get('status')}; "
            "UI service waits for R5 systemd ownership."
        )
    elif duplicate_ui:
        status = "BLOCKED_DUPLICATE_UI_PROCESS"
        ready = False
        reason = "More than one UI process is already running on the cloud host."
    elif ui_state.get("service_started") and ui_state.get("local_ui_http_ok"):
        status = "UI_ALREADY_RUNNING"
        ready = False
        reason = "The UI service already appears active and locally reachable."
    else:
        status = "DRAFT_READY_FOR_REVIEW"
        ready = True
        reason = (
            "R5 is owned by systemd and no duplicate UI process was detected. "
            "Draft a local-only UI service before any install/start phase."
        )
    app_path = str(target.get("app_path") or "/opt/kalshi-predictive-bot")
    return {
        "status": status,
        "ready_for_review": ready,
        "install_allowed_now": False,
        "enable_allowed_now": False,
        "start_allowed_now": False,
        "expose_public_allowed_now": False,
        "ui_service_name": ui_service_name,
        "ui_bind_host": ui_host,
        "ui_bind_port": ui_port,
        "ui_command": f"{app_path}/.venv/bin/kalshi-bot ui --host {ui_host} --port {ui_port}",
        "remote_app_path": app_path,
        "remote_env_path": target.get("env_path") or "/etc/kalshi-bot/kalshi-bot.env",
        "remote_db_path": target.get("db_path") or "/var/lib/kalshi-bot/kalshi_phase1.db",
        "remote_reports_path": target.get("reports_path") or f"{app_path}/reports",
        "ssh_target": target.get("ssh_target"),
        "identity_file": target.get("identity_file"),
        "r18_status": r18_decision.get("status"),
        "r5_pid": r18_decision.get("current_r5_pid"),
        "primary_reason": reason,
        "access_plan": "SSH_TUNNEL_FIRST_REVERSE_PROXY_DEFERRED",
        "ssh_tunnel_command": _ssh_tunnel_command(target, ui_port),
        "operator_next_command": (
            "kalshi-bot phase3bb-r20-cloud-ui-service-plan "
            "--output-dir reports/phase3bb_r20 --reports-dir reports"
        ),
        "next_codex_step": "Phase 3BB-R21 - Cloud UI Install Review / No-start Dry Run",
    }


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R20 Cloud UI Service Plan")
    plan = payload["ui_service_plan"]
    ui_state = payload["parsed_ui_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{plan['status']}`",
            f"- Ready for review: `{plan['ready_for_review']}`",
            f"- Install allowed now: `{plan['install_allowed_now']}`",
            f"- Start allowed now: `{plan['start_allowed_now']}`",
            f"- Public exposure allowed now: `{plan['expose_public_allowed_now']}`",
            f"- R18 status: `{plan['r18_status']}`",
            f"- R5 PID: `{plan['r5_pid']}`",
            f"- UI service active: `{ui_state['service_started']}`",
            f"- UI local HTTP OK: `{ui_state['local_ui_http_ok']}`",
            f"- Reason: {plan['primary_reason']}",
            "",
            "## Draft Artifacts",
            "",
            f"- Service draft: `{plan['ui_service_name']}.draft`",
            "- Nginx draft: `kalshi-ui.nginx.draft`",
            "- Install checklist: `ui_install_review_checklist.md`",
            "",
            "## Safety",
            "",
            "- No UI service was installed, enabled, or started.",
            "- No public firewall or nginx change was made.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Command",
            "",
            "```bash",
            plan["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {plan['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R20 Cloud UI Service Plan Detail")
    plan = payload["ui_service_plan"]
    ui_state = payload["parsed_ui_state"]
    lines.extend(
        [
            "",
            "## Strategy",
            "",
            "Run the operator UI as a local-only service on the droplet, bound to "
            "`127.0.0.1:8080`. First access should use an SSH tunnel. Public nginx/TLS "
            "exposure stays deferred until a separate review phase.",
            "",
            "## Plan",
            "",
            f"- Status: `{plan['status']}`",
            f"- Service name: `{plan['ui_service_name']}`",
            f"- UI command: `{plan['ui_command']}`",
            f"- Access plan: `{plan['access_plan']}`",
            f"- SSH tunnel: `{plan['ssh_tunnel_command']}`",
            "",
            "## Current Remote UI State",
            "",
            f"- Service loaded: `{ui_state['service_loaded']}`",
            f"- Service enabled: `{ui_state['service_enabled']}`",
            f"- Service active: `{ui_state['service_started']}`",
            f"- UI process pids: `{ui_state['ui_process_pids']}`",
            f"- Port 8080 listening: `{ui_state['ui_port_listening']}`",
            f"- Public HTTP listening: `{ui_state['public_http_listening']}`",
            f"- Public HTTPS listening: `{ui_state['public_https_listening']}`",
            f"- Nginx present: `{ui_state['nginx_present']}`",
            f"- Local UI HTTP OK: `{ui_state['local_ui_http_ok']}`",
            "",
            "## Review Gates Before Any Install",
            "",
            "- R18 status remains `SYSTEMD_OWNS_R5`.",
            "- UI binds to `127.0.0.1`, not `0.0.0.0`.",
            "- `UI_READ_ONLY=true`, `EXECUTION_ENABLED=false`, and "
            "`EXECUTION_DRY_RUN=true` stay set.",
            "- No duplicate UI process is running.",
            "- Public reverse proxy and firewall changes are reviewed separately.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_service_draft(payload: dict[str, Any]) -> str:
    plan = payload["ui_service_plan"]
    return "\n".join(
        [
            "[Unit]",
            "Description=Kalshi Bot operator UI (paper/read-only)",
            "Wants=network-online.target",
            "After=network-online.target kalshi-r5-watcher.service",
            "Requires=kalshi-r5-watcher.service",
            f"ConditionPathExists={plan['remote_env_path']}",
            f"ConditionPathExists={plan['remote_db_path']}",
            "",
            "[Service]",
            "Type=simple",
            "User=kalshi",
            f"WorkingDirectory={plan['remote_app_path']}",
            f"EnvironmentFile={plan['remote_env_path']}",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=UI_READ_ONLY=true",
            "Environment=EXECUTION_ENABLED=false",
            "Environment=EXECUTION_DRY_RUN=true",
            "Environment=EXECUTION_KILL_SWITCH=true",
            f"ExecStart={plan['ui_command']}",
            "Restart=always",
            "RestartSec=10",
            "TimeoutStopSec=30",
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


def _render_nginx_draft(payload: dict[str, Any]) -> str:
    plan = payload["ui_service_plan"]
    return "\n".join(
        [
            "# Draft only. Do not install until reviewed in a later phase.",
            "# Recommended first access is the SSH tunnel, not public exposure.",
            "server {",
            "    listen 80;",
            "    server_name _;",
            "",
            "    # Replace this with the operator IP before installing, or keep nginx disabled.",
            "    # allow YOUR_PUBLIC_IP;",
            "    # deny all;",
            "",
            "    location / {",
            f"        proxy_pass http://127.0.0.1:{plan['ui_bind_port']};",
            "        proxy_http_version 1.1;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
            "}",
            "",
        ]
    )


def _render_install_checklist(payload: dict[str, Any]) -> str:
    plan = payload["ui_service_plan"]
    lines = _metadata_lines(payload, "# Phase 3BB-R20 UI Install Review Checklist")
    lines.extend(
        [
            "",
            "## R20 Is Draft Only",
            "",
            "- [ ] Confirm R18 still reports `SYSTEMD_OWNS_R5`.",
            "- [ ] Confirm no existing duplicate UI process is running.",
            "- [ ] Review `kalshi-ui.service.draft`.",
            "- [ ] Confirm the UI binds to `127.0.0.1:8080`.",
            "- [ ] Confirm read-only environment flags are present.",
            "- [ ] Use SSH tunnel access first.",
            "- [ ] Defer nginx/firewall/public exposure until the review phase.",
            "",
            "## Access After Install",
            "",
            "```bash",
            plan["ssh_tunnel_command"],
            "```",
            "",
            "Then open `http://127.0.0.1:8080` locally.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    plan = payload["ui_service_plan"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# R20 is draft-only. Re-run before any UI install/start phase.",
            plan["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R20 Next Actions")
    plan = payload["ui_service_plan"]
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
            "- Do not install, enable, or start the UI service from R20.",
            "- Do not expose port 8080 publicly.",
            "- Do not install the nginx draft yet.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _target_from_payload(payload: dict[str, Any]) -> Any:
    from kalshi_predictor.phase3bb_r12_cloud_bootstrap import CloudBootstrapTarget

    return CloudBootstrapTarget(
        ssh_target=str(payload.get("ssh_target") or ""),
        identity_file=str(payload.get("identity_file") or ""),
        app_path=str(payload.get("app_path") or ""),
        env_path=str(payload.get("env_path") or ""),
        db_path=str(payload.get("db_path") or ""),
        reports_path=str(payload.get("reports_path") or ""),
    )


def _ssh_tunnel_command(target: dict[str, Any], ui_port: int) -> str:
    identity = str(target.get("identity_file") or "~/.ssh/id_ed25519_do")
    ssh_target = str(target.get("ssh_target") or "kalshi@159.65.35.72")
    return (
        f"ssh -i {_shell_quote(identity)} -L {ui_port}:127.0.0.1:{ui_port} "
        f"{_shell_quote(ssh_target)}"
    )


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _pids_from_process_output(text: str) -> list[int]:
    pids: list[int] = []
    for line in text.splitlines():
        if "pgrep -af" in line or "ps -eo pid=,args=" in line or "awk " in line:
            continue
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        pid = _to_int(parts[0])
        if pid is not None:
            pids.append(pid)
    return pids


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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
