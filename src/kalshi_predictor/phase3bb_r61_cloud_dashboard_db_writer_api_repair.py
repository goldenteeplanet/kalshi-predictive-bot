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
    _json_from_probe,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r24_cloud_ui_start_tunnel_verification import (
    DEFAULT_UI_PORT,
    DEFAULT_UI_SERVICE_NAME,
)
from kalshi_predictor.phase3bb_r32_cloud_ui_dashboard_truth_scheduler_status import (
    DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    DEFAULT_UI_TIMEOUT_SECONDS,
    UiApiProbe,
    UiApiProbeRunner,
    _run_ui_api_probe,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R61_VERSION = "phase3bb_r61_cloud_dashboard_db_writer_api_repair_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r61")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 30
PRIVATE_API_PATHS = (
    ("db_writer_api", "/api/db-writer-monitor"),
    ("workspace_guard_api", "/api/workspace-guard"),
    ("dashboard_snapshot_api", "/api/dashboard/v1/snapshots/current"),
)


@dataclass(frozen=True)
class Phase3BBR61CloudDashboardDbWriterApiRepairArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    ui_api_probe_csv_path: Path
    checks_csv_path: Path
    ui_start_handoff_path: Path
    scheduler_no_start_handoff_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r61_cloud_dashboard_db_writer_api_repair_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
) -> Phase3BBR61CloudDashboardDbWriterApiRepairArtifacts:
    payload = build_phase3bb_r61_cloud_dashboard_db_writer_api_repair(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        private_base_url=private_base_url,
        ui_timeout_seconds=ui_timeout_seconds,
        max_dashboard_age_seconds=max_dashboard_age_seconds,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
        ui_probe_runner=ui_probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_dashboard_db_writer_api_repair.md"
    json_path = output_dir / "cloud_dashboard_db_writer_api_repair.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    ui_api_probe_csv_path = output_dir / "private_ui_api_probe_results.csv"
    checks_csv_path = output_dir / "repair_checks.csv"
    ui_start_handoff_path = output_dir / "operator_ui_start_handoff.sh"
    scheduler_no_start_handoff_path = output_dir / "operator_r60_scheduler_no_start_handoff.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(ui_api_probe_csv_path, payload["private_ui_api_probe_results"])
    _write_rows_csv(checks_csv_path, payload["repair_checks"])
    ui_start_handoff_path.write_text(_render_ui_start_handoff(payload), encoding="utf-8")
    _mark_executable(ui_start_handoff_path)
    scheduler_no_start_handoff_path.write_text(
        _render_scheduler_no_start_handoff(payload),
        encoding="utf-8",
    )
    _mark_executable(scheduler_no_start_handoff_path)
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
            ui_api_probe_csv_path,
            checks_csv_path,
            ui_start_handoff_path,
            scheduler_no_start_handoff_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR61CloudDashboardDbWriterApiRepairArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        ui_api_probe_csv_path=ui_api_probe_csv_path,
        checks_csv_path=checks_csv_path,
        ui_start_handoff_path=ui_start_handoff_path,
        scheduler_no_start_handoff_path=scheduler_no_start_handoff_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r61_cloud_dashboard_db_writer_api_repair(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    ui_timeout_seconds: int = DEFAULT_UI_TIMEOUT_SECONDS,
    max_dashboard_age_seconds: int = DEFAULT_MAX_DASHBOARD_AGE_SECONDS,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
    ui_probe_runner: UiApiProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r61-cloud-dashboard-db-writer-api-reachability-repair",
        "argv": command_args or [],
    }
    r11 = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    r32 = _read_json(reports_dir / "phase3bb_r32" / "cloud_ui_dashboard_truth_scheduler_status.json")
    r33 = _read_json(reports_dir / "phase3bb_r33" / "cloud_paper_only_operations_readiness.json")
    r34 = _read_json(reports_dir / "phase3bb_r34" / "cloud_multicategory_refresh_scheduler_review.json")
    target = _resolve_target(
        r11,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    resolved_base_url = _resolve_private_base_url(private_base_url, r32, reports_dir)
    runner = probe_runner or _run_ssh_probe
    remote_probes = _build_remote_probes(
        target,
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        timeout_seconds=per_probe_timeout_seconds,
    )
    remote_results = [runner(probe, target) for probe in remote_probes]
    parsed_remote = _parse_remote_state(remote_results, ui_service_name=ui_service_name)
    ui_runner = ui_probe_runner or _run_ui_api_probe
    ui_results = [
        ui_runner(UiApiProbe(name, path, ui_timeout_seconds), resolved_base_url)
        for name, path in PRIVATE_API_PATHS
    ] if resolved_base_url else []
    ui_payloads = [_ui_probe_payload(row) for row in ui_results]
    checks = _repair_checks(
        parsed_remote=parsed_remote,
        ui_results=ui_payloads,
        r32=r32,
        r33=r33,
        r34=r34,
    )
    decision = _repair_decision(
        checks=checks,
        parsed_remote=parsed_remote,
        r32=r32,
        r33=r33,
        r34=r34,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "no_start": True,
        "no_install": True,
        "ssh_read_only_commands_executed": len(remote_results),
        "ssh_mutating_commands_executed": 0,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "starts_ui_service": False,
        "starts_scheduler": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "thresholds_lowered": False,
    }
    return {
        **metadata,
        "phase": "3BB-R61-CLOUD-DASHBOARD-DB-WRITER-API-REACHABILITY-REPAIR",
        "phase_version": PHASE3BB_R61_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_API_DIAGNOSTIC_NO_START",
        "reports_dir": str(reports_dir),
        "cloud_target": {
            "ssh_target": target.ssh_target,
            "identity_file": target.identity_file,
            "app_path": target.app_path,
            "env_path": target.env_path,
            "db_path": target.db_path,
            "reports_path": target.reports_path,
        },
        "private_base_url": resolved_base_url,
        "ui_service_name": ui_service_name,
        "ui_port": ui_port,
        "max_dashboard_age_seconds": max_dashboard_age_seconds,
        "prior_r32_decision": r32.get("verification_decision") or {},
        "prior_r33_decision": _r33_decision(r33),
        "prior_r34_decision": r34.get("scheduler_decision") or {},
        "remote_probe_results": [_result_payload(row) for row in remote_results],
        "parsed_remote_state": parsed_remote,
        "private_ui_api_probe_results": ui_payloads,
        "repair_checks": checks,
        "repair_decision": decision,
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
    ui_service_name: str,
    ui_port: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(ui_service_name)
    port = int(ui_port)
    source_env = f"set -a && . {env} && set +a"
    return [
        RemoteProbe(
            "ui_systemd_state",
            (
                f"systemctl show {service} --no-pager "
                "-p Id -p LoadState -p UnitFileState -p ActiveState -p SubState "
                "-p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "ui_local_listener",
            f"ss -ltnp 2>/dev/null | grep -E ':{port}\\b|:8081\\b' || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "ui_loopback_db_writer_api",
            f"curl -sS -m 10 -i http://127.0.0.1:{port}/api/db-writer-monitor | head -80",
            timeout_seconds,
        ),
        RemoteProbe(
            "tailscale_serve_status",
            "tailscale serve status 2>&1 || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "remote_r60_registry",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bb-r60-weather-next-window-lead-time-"
                "scheduler-repair --help >/dev/null && echo R60_REGISTERED"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "remote_r32_r33_r34_registry",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-"
                "scheduler-status-verification --help >/dev/null && "
                ".venv/bin/kalshi-bot phase3bb-r33-cloud-paper-only-operations-"
                "readiness --help >/dev/null && "
                ".venv/bin/kalshi-bot phase3bb-r34-cloud-multicategory-refresh-"
                "scheduler-review --help >/dev/null && echo R32_R33_R34_REGISTERED"
            ),
            timeout_seconds,
        ),
    ]


def _parse_remote_state(
    results: list[Any],
    *,
    ui_service_name: str,
) -> dict[str, Any]:
    by_name = {row.name: row for row in results}
    systemd = _parse_systemctl_show(by_name.get("ui_systemd_state").stdout if by_name.get("ui_systemd_state") else "")
    listener_text = by_name.get("ui_local_listener").stdout if by_name.get("ui_local_listener") else ""
    loopback = by_name.get("ui_loopback_db_writer_api")
    loopback_status = _http_status_from_text(loopback.stdout if loopback else "")
    tailscale_text = by_name.get("tailscale_serve_status").stdout if by_name.get("tailscale_serve_status") else ""
    return {
        "ui_service_name": ui_service_name,
        "ui_service_load_state": systemd.get("LoadState"),
        "ui_service_unit_file_state": systemd.get("UnitFileState"),
        "ui_service_active_state": systemd.get("ActiveState"),
        "ui_service_sub_state": systemd.get("SubState"),
        "ui_service_exec_main_pid": _int_or_none(systemd.get("ExecMainPID")),
        "ui_service_loaded": systemd.get("LoadState") == "loaded",
        "ui_service_active": systemd.get("ActiveState") == "active",
        "ui_port_listening": bool(listener_text.strip()),
        "ui_listener_excerpt": listener_text.strip()[:500],
        "loopback_db_writer_http_status": loopback_status,
        "loopback_db_writer_reachable": bool(loopback and loopback.ok and loopback_status == 200),
        "loopback_db_writer_json": _json_from_probe(loopback) if loopback else {},
        "tailscale_serve_configured": "proxy http://127.0.0.1:8080" in tailscale_text
        or "127.0.0.1:8080" in tailscale_text,
        "tailscale_serve_excerpt": tailscale_text.strip()[:1000],
        "r60_registered_on_cloud": "R60_REGISTERED" in (
            by_name.get("remote_r60_registry").stdout if by_name.get("remote_r60_registry") else ""
        ),
        "r32_r33_r34_registered_on_cloud": "R32_R33_R34_REGISTERED" in (
            by_name.get("remote_r32_r33_r34_registry").stdout
            if by_name.get("remote_r32_r33_r34_registry")
            else ""
        ),
    }


def _repair_checks(
    *,
    parsed_remote: dict[str, Any],
    ui_results: list[dict[str, Any]],
    r32: dict[str, Any],
    r33: dict[str, Any],
    r34: dict[str, Any],
) -> list[dict[str, Any]]:
    result_by_name = {row["name"]: row for row in ui_results}
    r32_decision = r32.get("verification_decision") or {}
    r33_decision = _r33_decision(r33)
    r34_decision = r34.get("scheduler_decision") or {}
    return [
        _check(
            "ui_service_loaded",
            bool(parsed_remote.get("ui_service_loaded")),
            f"LoadState={parsed_remote.get('ui_service_load_state')}.",
        ),
        _check(
            "ui_service_active",
            bool(parsed_remote.get("ui_service_active")),
            f"ActiveState={parsed_remote.get('ui_service_active_state')}; SubState={parsed_remote.get('ui_service_sub_state')}.",
        ),
        _check(
            "ui_port_listening",
            bool(parsed_remote.get("ui_port_listening")),
            f"Listener={parsed_remote.get('ui_listener_excerpt') or 'none'}.",
        ),
        _check(
            "loopback_db_writer_api_reachable",
            bool(parsed_remote.get("loopback_db_writer_reachable")),
            f"HTTP status={parsed_remote.get('loopback_db_writer_http_status')}.",
        ),
        _check(
            "private_db_writer_api_reachable",
            bool((result_by_name.get("db_writer_api") or {}).get("ok")),
            f"HTTP status={(result_by_name.get('db_writer_api') or {}).get('status_code')}.",
        ),
        _check(
            "private_workspace_guard_api_reachable",
            bool((result_by_name.get("workspace_guard_api") or {}).get("ok")),
            f"HTTP status={(result_by_name.get('workspace_guard_api') or {}).get('status_code')}.",
        ),
        _check(
            "private_dashboard_snapshot_api_reachable",
            bool((result_by_name.get("dashboard_snapshot_api") or {}).get("ok")),
            f"HTTP status={(result_by_name.get('dashboard_snapshot_api') or {}).get('status_code')}.",
        ),
        _check(
            "r32_r33_r34_registered_on_cloud",
            bool(parsed_remote.get("r32_r33_r34_registered_on_cloud")),
            "Cloud CLI help contains R32/R33/R34 commands.",
        ),
        _check(
            "r60_registered_on_cloud",
            bool(parsed_remote.get("r60_registered_on_cloud")),
            "Cloud CLI help contains R60 weather lead-time command.",
        ),
        _check(
            "latest_r32_verified",
            r32_decision.get("status") == "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS",
            f"R32 status={r32_decision.get('status')}.",
        ),
        _check(
            "latest_r33_ready",
            r33_decision.get("status") in {"PAPER_ONLY_MONITORING_READY", "PAPER_ONLY_OPERATOR_REVIEW_READY"},
            f"R33 status={r33_decision.get('status')}.",
        ),
        _check(
            "latest_r34_ready_for_no_start_handoff",
            r34_decision.get("status") == "READY_FOR_NO_START_SCHEDULER_DRY_RUN",
            f"R34 status={r34_decision.get('status')}.",
        ),
    ]


def _repair_decision(
    *,
    checks: list[dict[str, Any]],
    parsed_remote: dict[str, Any],
    r32: dict[str, Any],
    r33: dict[str, Any],
    r34: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r32_decision = r32.get("verification_decision") or {}
    r33_decision = _r33_decision(r33)
    r34_decision = r34.get("scheduler_decision") or {}
    if not parsed_remote.get("ui_service_loaded"):
        status = "BLOCKED_UI_SERVICE_NOT_INSTALLED"
        reason = "kalshi-ui.service is not loaded on the cloud host."
        next_step = "Phase 3BB-R22 - Operator-Approved Cloud UI Install Handoff"
        command = "kalshi-bot phase3bb-r22-cloud-ui-install-handoff --output-dir reports/phase3bb_r22 --reports-dir reports"
    elif not parsed_remote.get("ui_service_active") or not parsed_remote.get("ui_port_listening"):
        status = "BLOCKED_UI_BACKEND_INACTIVE"
        reason = "Tailscale Serve is up, but no localhost UI backend is serving /api/db-writer-monitor."
        next_step = "Phase 3BB-R24 - Operator-Approved Cloud UI Start + SSH Tunnel Verification"
        command = (
            "kalshi-bot phase3bb-r24-cloud-ui-start-tunnel-verification "
            "--output-dir reports/phase3bb_r24 --reports-dir reports --operator-approved"
        )
    elif not parsed_remote.get("loopback_db_writer_reachable"):
        status = "BLOCKED_LOOPBACK_DB_WRITER_API"
        reason = "The UI service is running, but localhost /api/db-writer-monitor is not returning HTTP 200."
        next_step = "Phase 3BB-R61 - Inspect UI service logs and route errors"
        command = "journalctl -u kalshi-ui.service -n 120 --no-pager"
    elif any(row["check"].startswith("private_") and not row["passed"] for row in checks):
        status = "BLOCKED_PRIVATE_UI_TAILSCALE_REACHABILITY"
        reason = "The cloud loopback API is healthy, but private Tailscale API probes still fail."
        next_step = "Phase 3BB-R30/R31 - Private Access Verification"
        command = "kalshi-bot phase3bb-r30-cloud-ui-private-access-install-verification --output-dir reports/phase3bb_r30 --reports-dir reports"
    elif r32_decision.get("status") != "VERIFIED_DASHBOARD_TRUTH_AND_SCHEDULER_STATUS":
        status = "READY_TO_RERUN_R32"
        reason = "UI APIs are reachable; refresh dashboard truth before scheduler handoff."
        next_step = "Phase 3BB-R32 - Cloud UI Dashboard Truth And Scheduler Status Verification"
        command = "kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-verification --output-dir reports/phase3bb_r32 --reports-dir reports"
    elif r33_decision.get("status") not in {"PAPER_ONLY_MONITORING_READY", "PAPER_ONLY_OPERATOR_REVIEW_READY"}:
        status = "READY_TO_RERUN_R33"
        reason = "R32 is verified; refresh paper-only operations readiness."
        next_step = "Phase 3BB-R33 - Cloud Paper-Only Operations Readiness Monitor"
        command = "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness --output-dir reports/phase3bb_r33 --reports-dir reports"
    elif r34_decision.get("status") != "READY_FOR_NO_START_SCHEDULER_DRY_RUN":
        status = "READY_TO_RERUN_R34"
        reason = "R33 is ready; refresh the multi-category scheduler review with the R60 job."
        next_step = "Phase 3BB-R34 - Cloud Multi-Category Refresh Scheduler Review"
        command = "kalshi-bot phase3bb-r34-cloud-multicategory-refresh-scheduler-review --output-dir reports/phase3bb_r34 --reports-dir reports"
    else:
        status = "READY_FOR_R60_SCHEDULER_NO_START_HANDOFF"
        reason = "UI API, R32, R33, R34, and R60 command registration are all clear."
        next_step = "Phase 3BB-R35 - Cloud Multi-Category Scheduler No-Start Dry Run"
        command = "kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run --output-dir reports/phase3bb_r35 --reports-dir reports"
    return {
        "status": status,
        "repair_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "prior_r32_status": r32_decision.get("status"),
        "prior_r33_status": r33_decision.get("status"),
        "prior_r34_status": r34_decision.get("status"),
        "ui_service_active": bool(parsed_remote.get("ui_service_active")),
        "ui_port_listening": bool(parsed_remote.get("ui_port_listening")),
        "loopback_db_writer_reachable": bool(parsed_remote.get("loopback_db_writer_reachable")),
        "r60_registered_on_cloud": bool(parsed_remote.get("r60_registered_on_cloud")),
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _resolve_private_base_url(
    private_base_url: str | None,
    r32: dict[str, Any],
    reports_dir: Path,
) -> str:
    if private_base_url:
        return private_base_url.rstrip("/")
    r32_url = str(r32.get("private_base_url") or "").strip()
    if r32_url:
        return r32_url.rstrip("/")
    r31 = _read_json(reports_dir / "phase3bb_r31" / "cloud_ui_private_access_operator_smoke_test.json")
    r31_url = str((r31.get("smoke_decision") or {}).get("private_base_url") or "").strip()
    return r31_url.rstrip("/") or "https://kalshi-bot-01.taile570d1.ts.net"


def _r33_decision(payload: dict[str, Any]) -> dict[str, Any]:
    return (
        payload.get("readiness_decision")
        or payload.get("operations_readiness_decision")
        or payload.get("operations_decision")
        or {}
    )


def _parse_systemctl_show(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _http_status_from_text(text: str) -> int | None:
    for line in text.splitlines():
        if line.upper().startswith("HTTP/"):
            pieces = line.split()
            if len(pieces) >= 2:
                return _int_or_none(pieces[1])
    return None


def _ui_probe_payload(result: Any) -> dict[str, Any]:
    return {
        "name": result.name,
        "path": result.path,
        "url": result.url,
        "ok": result.ok,
        "status_code": result.status_code,
        "content_type": result.content_type,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R61 Cloud Dashboard DB Writer API Repair")
    decision = payload["repair_decision"]
    parsed = payload["parsed_remote_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- UI service active: `{decision['ui_service_active']}`",
            f"- UI port listening: `{decision['ui_port_listening']}`",
            f"- Loopback DB writer API reachable: `{decision['loopback_db_writer_reachable']}`",
            f"- R60 registered on cloud: `{decision['r60_registered_on_cloud']}`",
            f"- Tailscale Serve configured: `{parsed.get('tailscale_serve_configured')}`",
            "",
            "## Next Command",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "## Safety",
            "",
            "- Read-only cloud probes only.",
            "- Did not start/stop UI, R5, scheduler, Tailscale, nginx, or firewall services.",
            "- Did not create paper/live/demo trades.",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R61 Detail")
    lines.extend(["", "## Checks", ""])
    for row in payload["repair_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Parsed Remote State", "", "```json"])
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.append("```")
    lines.extend(["", "## Private UI API Probes", ""])
    for row in payload["private_ui_api_probe_results"]:
        lines.append(
            f"- `{row['name']}` `{row['path']}` ok=`{row['ok']}` "
            f"status=`{row['status_code']}` error=`{row['error']}`"
        )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["repair_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R61 Next Actions")
    lines.extend(
        [
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Run Next",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "## R60 No-Start Scheduler Hook",
            "",
            "- Once R32/R33/R34 are verified, run `reports/phase3bb_r61/operator_r60_scheduler_no_start_handoff.sh` to generate the no-start scheduler draft.",
            "- Do not install or start the scheduler from R61.",
            "",
            "## Do Not Run",
            "",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not start duplicate R5 watchers.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            payload["repair_decision"]["operator_next_command"],
            "",
        ]
    )


def _render_ui_start_handoff(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r61] operator-approved UI start verification handoff'",
            "echo '[phase3bb-r61] this starts only the localhost-bound cloud UI via R24'",
            (
                "kalshi-bot phase3bb-r24-cloud-ui-start-tunnel-verification "
                "--output-dir reports/phase3bb_r24 --reports-dir reports --operator-approved"
            ),
            (
                "kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-"
                "verification --output-dir reports/phase3bb_r32 --reports-dir reports"
            ),
            "",
        ]
    )


def _render_scheduler_no_start_handoff(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r61] R60 scheduler no-start handoff'",
            "echo '[phase3bb-r61] this generates local scheduler drafts only; no install/start'",
            (
                "kalshi-bot phase3bb-r32-cloud-ui-dashboard-truth-scheduler-status-"
                "verification --output-dir reports/phase3bb_r32 --reports-dir reports"
            ),
            (
                "kalshi-bot phase3bb-r33-cloud-paper-only-operations-readiness "
                "--output-dir reports/phase3bb_r33 --reports-dir reports"
            ),
            (
                "kalshi-bot phase3bb-r34-cloud-multicategory-refresh-scheduler-review "
                "--output-dir reports/phase3bb_r34 --reports-dir reports"
            ),
            (
                "kalshi-bot phase3bb-r35-cloud-multicategory-scheduler-no-start-dry-run "
                "--output-dir reports/phase3bb_r35 --reports-dir reports"
            ),
            "",
        ]
    )


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass
