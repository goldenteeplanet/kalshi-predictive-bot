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
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _json_from_probe,
    _loose_db_writer_state,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r17_cloud_service_install_verification import (
    _resolve_verification_target,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R18_VERSION = "phase3bb_r18_cloud_scheduler_runtime_cutover_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r18")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SERVICE_NAME = "kalshi-r5-watcher.service"
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class Phase3BBR18CloudSchedulerRuntimeCutoverArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r18_cloud_scheduler_runtime_cutover_report(
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
    service_name: str = DEFAULT_SERVICE_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR18CloudSchedulerRuntimeCutoverArtifacts:
    payload = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
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
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_runtime_cutover.md"
    json_path = output_dir / "cloud_scheduler_runtime_cutover.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "runtime_cutover_checks.csv"
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
    _write_checks_csv(checks_csv_path, payload["runtime_cutover_checks"])
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
    return Phase3BBR18CloudSchedulerRuntimeCutoverArtifacts(
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


def build_phase3bb_r18_cloud_scheduler_runtime_cutover(
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
    service_name: str = DEFAULT_SERVICE_NAME,
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
        "command": "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r13_path = reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json"
    r14_path = reports_dir / "phase3bb_r14" / "cloud_service_plan.json"
    r17_path = reports_dir / "phase3bb_r17" / "cloud_service_install_verification.json"
    r11 = _read_json(r11_path)
    r13 = _read_json(r13_path)
    r14 = _read_json(r14_path)
    r17 = _read_json(r17_path)
    target = _resolve_verification_target(
        r11,
        r13,
        r14,
        r17,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    expected_pid = _expected_existing_r5_pid(r13, r17)
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(
        target,
        service_name=service_name,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    results = [runner(probe, target) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    parsed = _parse_probe_outputs(results)
    checks = _runtime_cutover_checks(
        r17=r17,
        parsed=parsed,
        expected_existing_r5_pid=expected_pid,
    )
    decision = _runtime_cutover_decision(
        checks,
        parsed=parsed,
        target=target,
        service_name=service_name,
        expected_existing_r5_pid=expected_pid,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "runtime_cutover_monitor_only": True,
        "remote_commands_executed": len(results),
        "remote_report_writes_only": True,
        "remote_db_writes_performed": 0,
        "systemctl_read_only_commands_executed": 3,
        "systemctl_mutating_commands_executed": 0,
        "service_files_written_to_system": False,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_r5_watcher": False,
        "starts_service": False,
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
        "phase": "3BB-R18-CLOUD-SCHEDULER-RUNTIME-CUTOVER",
        "phase_version": PHASE3BB_R18_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_RUNTIME_CUTOVER_MONITOR",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r13_artifact_path": str(r13_path),
        "r14_artifact_path": str(r14_path),
        "r17_artifact_path": str(r17_path),
        "r11_context_available": bool(r11),
        "r13_context_available": bool(r13),
        "r14_context_available": bool(r14),
        "r17_context_available": bool(r17),
        "cloud_target": {
            "ssh_target": target.ssh_target,
            "identity_file": target.identity_file,
            "app_path": target.app_path,
            "env_path": target.env_path,
            "db_path": target.db_path,
            "reports_path": target.reports_path,
        },
        "service_name": service_name,
        "expected_existing_r5_pid": expected_pid,
        "remote_probe_duration_seconds": duration,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "runtime_cutover_checks": checks,
        "runtime_cutover_decision": decision,
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
    service_name: str,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(service_name)
    source_env = f"set -a && . {env} && set +a"
    return [
        RemoteProbe(
            "systemd_unit",
            (
                f"systemctl show {service} --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe("systemd_enabled", f"systemctl is-enabled {service} || true", timeout_seconds),
        RemoteProbe("systemd_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r18_r5_status.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_guard_dry_run",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-unattended-guard "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r18_guard.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_unattended_guard.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "db_writer_monitor",
            f"cd {app} && {source_env} && .venv/bin/kalshi-bot db-writer-monitor --json",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_processes",
            "pgrep -af 'phase3bc-r5-crypto-freshness-watch' || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_pid_file",
            f"cd {app} && cat reports/phase3bc_r5/phase3bc_r5_unattended_job.pid || true",
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    systemd = _parse_systemd_show(_stdout(by_name.get("systemd_unit")))
    enabled = _first_line(_stdout(by_name.get("systemd_enabled")))
    active = _first_line(_stdout(by_name.get("systemd_active")))
    r5_status = _json_from_probe(by_name.get("r5_status"))
    guard = _json_from_probe(by_name.get("r5_guard_dry_run"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    r5_guard = r5_status.get("guard") if isinstance(r5_status, dict) else {}
    latest_summary = r5_status.get("latest_summary") if isinstance(r5_status, dict) else {}
    pids = [_to_int(pid) for pid in (process or {}).get("phase3bc_r5_pids") or []]
    pids = [pid for pid in pids if pid is not None]
    if not pids:
        pids = _pids_from_process_output(_stdout(by_name.get("r5_processes")))
    r5_status_pid = _to_int(r5_status.get("pid") if isinstance(r5_status, dict) else None)
    r5_status_pid_stale = bool(pids and r5_status_pid is not None and r5_status_pid not in pids)
    r5_pid = r5_status_pid
    if pids and (r5_pid is None or r5_status_pid_stale):
        r5_pid = pids[0]
    guard_after = guard.get("after") if isinstance(guard, dict) else {}
    guard_after_guard = guard_after.get("guard") if isinstance(guard_after, dict) else {}
    guard_status = (r5_guard or {}).get("status") or (guard_after_guard or {}).get(
        "status"
    ) or guard.get("status")
    guard_should_stop = bool(
        (r5_guard or {}).get("should_stop")
        or (guard_after_guard or {}).get("should_stop")
        or guard.get("should_stop")
    )
    writer_pid = _to_int(writer.get("current_writer_pid") if isinstance(writer, dict) else None)
    service_exec_main_pid = _to_int(systemd.get("ExecMainPID"))
    service_active = active or systemd.get("ActiveState")
    service_started = service_active == "active" or bool(service_exec_main_pid)
    service_owns_r5 = bool(
        service_started
        and service_exec_main_pid is not None
        and r5_pid is not None
        and service_exec_main_pid == r5_pid
    )
    pid_file_value = _to_int(_first_line(_stdout(by_name.get("r5_pid_file"))))
    return {
        "systemd_unit": systemd,
        "service_loaded": systemd.get("LoadState") == "loaded",
        "service_enabled_state": enabled or systemd.get("UnitFileState"),
        "service_enabled": (enabled or systemd.get("UnitFileState")) == "enabled",
        "service_active_state": service_active,
        "service_sub_state": systemd.get("SubState"),
        "service_exec_main_pid": service_exec_main_pid,
        "service_started": service_started,
        "service_owns_r5": service_owns_r5,
        "r5_status": r5_status,
        "guard_dry_run": guard,
        "db_writer_monitor": writer,
        "r5_running": bool((process or {}).get("phase3bc_r5_process_running")) or bool(pids),
        "r5_pids": pids,
        "r5_pid": r5_pid,
        "r5_status_pid": r5_status_pid,
        "r5_status_pid_stale": r5_status_pid_stale,
        "pid_file_value": pid_file_value,
        "duplicate_r5": len(pids) > 1,
        "guard_status": guard_status,
        "guard_should_stop": guard_should_stop,
        "watch_state": r5_status.get("latest_watch_state") if isinstance(r5_status, dict) else None,
        "paper_ready_candidates": (latest_summary or {}).get("paper_ready_candidates"),
        "positive_ev_rows": (latest_summary or {}).get("positive_ev_rows"),
        "writer_status": writer.get("status") if isinstance(writer, dict) else "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write"))
        if isinstance(writer, dict)
        else False,
        "writer_pid": writer_pid,
        "writer_clear_or_matches_r5": writer_pid is None or writer_pid == r5_pid,
        "writer_matches_r5": writer_pid is not None and writer_pid == r5_pid,
    }


def _runtime_cutover_checks(
    *,
    r17: dict[str, Any],
    parsed: dict[str, Any],
    expected_existing_r5_pid: int | None,
) -> list[dict[str, Any]]:
    r17_decision = r17.get("verification_decision") or {}
    r5_pid = _to_int(parsed.get("r5_pid"))
    service_started = bool(parsed.get("service_started"))
    return [
        _check(
            "r17_verified_service_install",
            r17_decision.get("status") == "VERIFIED_ENABLE_NO_START_HANDOFF",
            f"R17 status is {r17_decision.get('status')}.",
        ),
        _check(
            "service_loaded",
            bool(parsed.get("service_loaded")),
            f"LoadState={parsed.get('systemd_unit', {}).get('LoadState')}.",
        ),
        _check(
            "service_enabled",
            bool(parsed.get("service_enabled")),
            f"Service enabled state is {parsed.get('service_enabled_state')}.",
        ),
        _check(
            "no_duplicate_r5",
            not bool(parsed.get("duplicate_r5")),
            f"R5 PIDs: {parsed.get('r5_pids')}.",
        ),
        _check(
            "writer_not_conflicting",
            bool(parsed.get("writer_clear_or_matches_r5")),
            f"writer_pid={parsed.get('writer_pid')}; r5_pid={r5_pid}.",
        ),
        _check(
            "manual_r5_matches_expected_or_systemd_owns",
            (
                expected_existing_r5_pid is None
                or r5_pid == expected_existing_r5_pid
                or bool(parsed.get("service_owns_r5"))
                or not bool(parsed.get("r5_running"))
            ),
            f"Expected PID={expected_existing_r5_pid}; current PID={r5_pid}.",
        ),
        _check(
            "guard_not_overrun_when_r5_running",
            (
                not bool(parsed.get("r5_running"))
                or (
                    parsed.get("guard_status") == "RUNNING"
                    and parsed.get("guard_should_stop") is False
                )
            ),
            (
                f"guard_status={parsed.get('guard_status')}, "
                f"guard_should_stop={parsed.get('guard_should_stop')}."
            ),
        ),
        _check(
            "service_not_duplicate_owner",
            (
                not service_started
                or bool(parsed.get("service_owns_r5"))
                or not bool(parsed.get("r5_running"))
            ),
            (
                f"service_started={service_started}; service_pid="
                f"{parsed.get('service_exec_main_pid')}; r5_pid={r5_pid}."
            ),
        ),
    ]


def _runtime_cutover_decision(
    checks: list[dict[str, Any]],
    *,
    parsed: dict[str, Any],
    target: CloudBootstrapTarget,
    service_name: str,
    expected_existing_r5_pid: int | None,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    service_enabled = bool(parsed.get("service_enabled"))
    service_started = bool(parsed.get("service_started"))
    r5_running = bool(parsed.get("r5_running"))
    service_owns_r5 = bool(parsed.get("service_owns_r5"))
    if failed:
        status = "BLOCKED_RUNTIME_CUTOVER"
        action = "BLOCKED"
        reason = f"First failing check: {failed[0]['check']}."
        command = (
            "kalshi-bot phase3bb-r17-cloud-service-install-verification "
            "--output-dir reports/phase3bb_r17 --reports-dir reports"
        )
        next_step = "Phase 3BB-R17 - Resolve Service Verification Before Cutover"
    elif service_started and service_owns_r5:
        status = "SYSTEMD_OWNS_R5"
        action = "MONITOR_SYSTEMD_R5"
        reason = "The enabled systemd service is already the single R5 owner."
        command = _systemctl_status_command(target, service_name)
        next_step = "Phase 3BB-R20 - Cloud UI Service Plan"
    elif service_enabled and r5_running and not service_started:
        status = "WAIT_FOR_MANUAL_R5_TO_EXIT"
        action = "WAIT"
        reason = (
            "Service is installed/enabled but inactive while the adopted manual R5 "
            "watcher is still healthy. Do not start the service yet."
        )
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R18 - Continue Monitoring Until Manual R5 Exits"
    elif service_enabled and not r5_running and not service_started:
        status = "READY_FOR_SYSTEMD_START"
        action = "START_SERVICE_AFTER_R5_EXIT"
        reason = (
            "No R5 watcher is running and the enabled service can take ownership. "
            "This monitor did not start it."
        )
        command = _systemctl_start_command(target, service_name)
        next_step = "Phase 3BB-R19 - Operator Systemd Start, then rerun R18"
    else:
        status = "BLOCKED_RUNTIME_CUTOVER"
        action = "BLOCKED"
        reason = "Runtime state is not one of the expected cutover states."
        command = (
            "kalshi-bot phase3bb-r17-cloud-service-install-verification "
            "--output-dir reports/phase3bb_r17 --reports-dir reports"
        )
        next_step = "Phase 3BB-R17 - Reverify Cloud Service Install"
    return {
        "status": status,
        "recommended_action": action,
        "cutover_ready": status in {"READY_FOR_SYSTEMD_START", "SYSTEMD_OWNS_R5"},
        "monitor_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "service_enabled": service_enabled,
        "service_started": service_started,
        "service_active_state": parsed.get("service_active_state"),
        "service_owns_r5": service_owns_r5,
        "current_r5_pid": parsed.get("r5_pid"),
        "expected_existing_r5_pid": expected_existing_r5_pid,
        "duplicate_r5": bool(parsed.get("duplicate_r5")),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "writer_pid": parsed.get("writer_pid"),
        "writer_matches_r5": bool(parsed.get("writer_matches_r5")),
        "codex_executed_start": False,
        "codex_executed_stop": False,
        "codex_executed_service_change": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _expected_existing_r5_pid(r13: dict[str, Any], r17: dict[str, Any]) -> int | None:
    r17_decision = r17.get("verification_decision") or {}
    r13_decision = r13.get("adoption_decision") or {}
    return _to_int(r17_decision.get("current_r5_pid")) or _to_int(
        r13_decision.get("current_r5_pid")
    )


def _systemctl_start_command(target: CloudBootstrapTarget, service_name: str) -> str:
    return (
        f"ssh -i {_shell_quote(target.identity_file)} "
        f"{_shell_quote(target.ssh_target)} "
        f"{_shell_quote(f'sudo systemctl start {service_name}')}"
    )


def _systemctl_status_command(target: CloudBootstrapTarget, service_name: str) -> str:
    return (
        f"ssh -i {_shell_quote(target.identity_file)} "
        f"{_shell_quote(target.ssh_target)} "
        f"{_shell_quote(f'systemctl status {service_name} --no-pager')}"
    )


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


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


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _pids_from_process_output(text: str) -> list[int]:
    pids: list[int] = []
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        pid = _to_int(parts[0])
        if pid is not None:
            pids.append(pid)
    return pids


def _to_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R18 Cloud Scheduler Runtime Cutover")
    decision = payload["runtime_cutover_decision"]
    parsed = payload["parsed_remote_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Recommended action: `{decision['recommended_action']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Runtime State",
            "",
            f"- Service enabled: `{decision['service_enabled']}`",
            f"- Service started: `{decision['service_started']}`",
            f"- Service owns R5: `{decision['service_owns_r5']}`",
            f"- Existing R5 PID: `{decision['current_r5_pid']}`",
            f"- Duplicate R5: `{decision['duplicate_r5']}`",
            f"- Guard status: `{decision['guard_status']}`",
            f"- Guard should stop: `{decision['guard_should_stop']}`",
            f"- Watch state: `{parsed.get('watch_state')}`",
            f"- Positive EV rows: `{parsed.get('positive_ev_rows')}`",
            f"- Paper-ready candidates: `{parsed.get('paper_ready_candidates')}`",
            "",
            "## Safety",
            "",
            "- Codex did not start or stop the service.",
            "- Codex did not stop the existing R5 watcher.",
            "- No paper/live/demo trades were created.",
            "- No live/demo order submit/cancel/replace command was run.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R18 Runtime Cutover Detail")
    decision = payload["runtime_cutover_decision"]
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "This phase monitors the handoff from a manually launched R5 watcher to the "
            "enabled systemd service. It does not start or stop anything.",
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Recommended action: `{decision['recommended_action']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["runtime_cutover_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Parsed Remote State", "", "```json"])
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.extend(["```", "", "## Probe Results", ""])
    for result in payload["remote_probe_results"]:
        lines.append(
            f"- `{result['name']}` ok=`{result['ok']}` exit=`{result['exit_code']}` "
            f"duration=`{result['duration_seconds']}`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["runtime_cutover_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R18 next safe command.",
            command,
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R18 Next Actions")
    decision = payload["runtime_cutover_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Recommended action: `{decision['recommended_action']}`",
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
            "- Do not start the service while a healthy manual R5 watcher is active.",
            "- Do not stop the existing R5 watcher from this monitor phase.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


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


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "detail"]
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
