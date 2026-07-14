from __future__ import annotations

import csv
import json
import time
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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import ProbeRunner, _result_payload, _run_ssh_probe
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
)
from kalshi_predictor.phase3bb_r20_cloud_ui_service_plan import (
    DEFAULT_UI_PORT,
    DEFAULT_UI_SERVICE_NAME,
    _build_ui_probe_commands,
    _parse_ui_probe_results,
    _target_from_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R23_VERSION = "phase3bb_r23_cloud_ui_install_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r23")


@dataclass(frozen=True)
class Phase3BBR23CloudUiInstallVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r23_cloud_ui_install_verification_report(
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
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR23CloudUiInstallVerificationArtifacts:
    payload = build_phase3bb_r23_cloud_ui_install_verification(
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
        ui_port=ui_port,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_install_verification.md"
    json_path = output_dir / "cloud_ui_install_verification.json"
    probe_csv_path = output_dir / "remote_ui_probe_results.csv"
    checks_csv_path = output_dir / "ui_install_verification_checks.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_probe_csv(probe_csv_path, payload["remote_ui_probe_results"])
    _write_checks_csv(checks_csv_path, payload["verification_checks"])
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
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR23CloudUiInstallVerificationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r23_cloud_ui_install_verification(
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
        "command": "kalshi-bot phase3bb-r23-cloud-ui-install-verification",
        "argv": command_args or [],
    }
    r22_path = reports_dir / "phase3bb_r22" / "cloud_ui_install_handoff.json"
    r22 = _read_json(r22_path)
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
    target = _target_from_payload(dict(r18.get("cloud_target") or {}))
    probes = _build_ui_probe_commands(
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    results = [runner(probe, target) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    ui_state = _parse_ui_probe_results(results, ui_service_name=ui_service_name)
    checks = _verification_checks(r18=r18, r22=r22, ui_state=ui_state)
    decision = _verification_decision(checks, r18, r22, ui_state)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "post_operator_verification_only": True,
        "remote_commands_executed": len(results),
        "remote_report_writes_only": True,
        "remote_db_writes_performed": 0,
        "service_files_written_to_system": False,
        "systemctl_read_only_commands_executed": 3,
        "systemctl_mutating_commands_executed": 0,
        "ssh_commands_execute_read_only_probes": len(results),
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
        "phase": "3BB-R23-CLOUD-UI-INSTALL-VERIFICATION",
        "phase_version": PHASE3BB_R23_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_INSTALL_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r22_artifact_path": str(r22_path),
        "r22_context_available": bool(r22),
        "r18_preflight": r18,
        "remote_ui_probe_duration_seconds": duration,
        "remote_ui_probe_results": [_result_payload(result) for result in results],
        "parsed_ui_state": ui_state,
        "install_handoff_decision": r22.get("handoff_decision") or {},
        "verification_checks": checks,
        "verification_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _verification_checks(
    *,
    r18: dict[str, Any],
    r22: dict[str, Any],
    ui_state: dict[str, Any],
) -> list[dict[str, Any]]:
    r18_decision = r18.get("runtime_cutover_decision") or {}
    r22_decision = r22.get("handoff_decision") or {}
    return [
        _check("r22_handoff_artifact_present", bool(r22), "R22 handoff artifact exists."),
        _check(
            "r22_handoff_ready",
            r22_decision.get("status") == "HANDOFF_READY_UI_INSTALL_ENABLE_NO_START",
            f"R22 status is {r22_decision.get('status')}.",
        ),
        _check(
            "r18_systemd_owns_r5",
            r18_decision.get("status") == "SYSTEMD_OWNS_R5",
            f"R18 status is {r18_decision.get('status')}.",
        ),
        _check(
            "ui_service_loaded",
            bool(ui_state.get("service_loaded")),
            f"LoadState={ui_state.get('systemd_unit', {}).get('LoadState')}.",
        ),
        _check(
            "ui_service_enabled",
            bool(ui_state.get("service_enabled")),
            f"Service enabled state is {ui_state.get('service_enabled_state')}.",
        ),
        _check(
            "ui_service_not_started",
            not bool(ui_state.get("service_started")),
            f"ActiveState={ui_state.get('service_active_state')}; pid={ui_state.get('service_exec_main_pid')}.",
        ),
        _check(
            "no_duplicate_ui_process",
            not bool(ui_state.get("ui_duplicate_process")),
            f"UI PIDs: {ui_state.get('ui_process_pids')}.",
        ),
        _check(
            "ui_port_not_listening_before_start",
            not bool(ui_state.get("ui_port_listening")),
            f"Listeners: {ui_state.get('listener_text') or 'none'}.",
        ),
        _check(
            "no_public_http_https_exposure",
            not bool(ui_state.get("public_http_listening"))
            and not bool(ui_state.get("public_https_listening")),
            f"Listeners: {ui_state.get('listener_text') or 'none'}.",
        ),
    ]


def _verification_decision(
    checks: list[dict[str, Any]],
    r18: dict[str, Any],
    r22: dict[str, Any],
    ui_state: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r18_decision = r18.get("runtime_cutover_decision") or {}
    r22_decision = r22.get("handoff_decision") or {}
    if failed:
        status = "BLOCKED_UI_INSTALL_VERIFICATION"
        reason = f"First failing check: {failed[0]['check']}."
        next_command = (
            "kalshi-bot phase3bb-r23-cloud-ui-install-verification "
            "--output-dir reports/phase3bb_r23 --reports-dir reports"
        )
        next_step = "Phase 3BB-R23 - Resolve Cloud UI Install Verification"
    else:
        status = "VERIFIED_UI_ENABLE_NO_START_HANDOFF"
        reason = "The UI service is installed and enabled, but it has not been started or exposed publicly."
        next_command = "systemctl status kalshi-ui.service --no-pager"
        next_step = "Phase 3BB-R24 - Operator-Approved Cloud UI Start + SSH Tunnel Verification"
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r18_status": r18_decision.get("status"),
        "r22_status": r22_decision.get("status"),
        "r5_pid": r18_decision.get("current_r5_pid"),
        "ui_service_loaded": bool(ui_state.get("service_loaded")),
        "ui_service_enabled": bool(ui_state.get("service_enabled")),
        "ui_service_started": bool(ui_state.get("service_started")),
        "ui_port_listening": bool(ui_state.get("ui_port_listening")),
        "public_http_listening": bool(ui_state.get("public_http_listening")),
        "public_https_listening": bool(ui_state.get("public_https_listening")),
        "operator_next_command": next_command,
        "next_codex_step": next_step,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R23 Cloud UI Install Verification")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- R18 status: `{decision['r18_status']}`",
            f"- R22 status: `{decision['r22_status']}`",
            f"- UI service loaded: `{decision['ui_service_loaded']}`",
            f"- UI service enabled: `{decision['ui_service_enabled']}`",
            f"- UI service started: `{decision['ui_service_started']}`",
            f"- UI port listening: `{decision['ui_port_listening']}`",
            f"- Public HTTP/HTTPS listening: `{decision['public_http_listening']}` / `{decision['public_https_listening']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Codex did not start the UI service.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R23 Verification Detail")
    decision = payload["verification_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["verification_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Parsed UI State", "", "```json", json.dumps(payload["parsed_ui_state"], indent=2, sort_keys=True), "```"])
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["verification_decision"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", "", decision["operator_next_command"], ""])


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R23 Next Actions")
    decision = payload["verification_decision"]
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
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly yet.",
            "- Do not install nginx or open firewall ports yet.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
