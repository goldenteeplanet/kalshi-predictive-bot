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
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R17_VERSION = "phase3bb_r17_cloud_service_install_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r17")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_SERVICE_NAME = "kalshi-r5-watcher.service"
DEFAULT_GUARD_SCRIPT_PATH = "/opt/kalshi-predictive-bot/scripts/cloud/kalshi-r5-start-guard.sh"
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class Phase3BBR17CloudServiceInstallVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r17_cloud_service_install_verification_report(
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
    service_name: str | None = None,
    guard_script_path: str | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR17CloudServiceInstallVerificationArtifacts:
    payload = build_phase3bb_r17_cloud_service_install_verification(
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
        guard_script_path=guard_script_path,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_service_install_verification.md"
    json_path = output_dir / "cloud_service_install_verification.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "service_verification_checks.csv"
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
    return Phase3BBR17CloudServiceInstallVerificationArtifacts(
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


def build_phase3bb_r17_cloud_service_install_verification(
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
    service_name: str | None = None,
    guard_script_path: str | None = None,
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
        "command": "kalshi-bot phase3bb-r17-cloud-service-install-verification",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r13_path = reports_dir / "phase3bb_r13" / "cloud_scheduler_adoption.json"
    r14_path = reports_dir / "phase3bb_r14" / "cloud_service_plan.json"
    r15_path = reports_dir / "phase3bb_r15" / "cloud_service_install_review.json"
    r16_path = reports_dir / "phase3bb_r16" / "cloud_service_install_handoff.json"
    r11 = _read_json(r11_path)
    r13 = _read_json(r13_path)
    r14 = _read_json(r14_path)
    r15 = _read_json(r15_path)
    r16 = _read_json(r16_path)
    service_plan = r14.get("service_plan") or {}
    target = _resolve_verification_target(
        r11,
        r13,
        r14,
        r16,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    resolved_service_name = service_name or str(
        service_plan.get("service_name") or DEFAULT_SERVICE_NAME
    )
    resolved_guard_path = guard_script_path or str(
        service_plan.get("guard_script_path") or DEFAULT_GUARD_SCRIPT_PATH
    )
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(
        target,
        service_name=resolved_service_name,
        guard_script_path=resolved_guard_path,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    results = [runner(probe, target) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    parsed = _parse_probe_outputs(results)
    expected_r5_pid = _expected_r5_pid(r13, r16)
    checks = _verification_checks(
        r13=r13,
        r14=r14,
        r15=r15,
        r16=r16,
        parsed=parsed,
        expected_r5_pid=expected_r5_pid,
    )
    decision = _verification_decision(checks, parsed, expected_r5_pid=expected_r5_pid)
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
        "phase": "3BB-R17-CLOUD-SERVICE-INSTALL-VERIFICATION",
        "phase_version": PHASE3BB_R17_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SERVICE_INSTALL_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r13_artifact_path": str(r13_path),
        "r14_artifact_path": str(r14_path),
        "r15_artifact_path": str(r15_path),
        "r16_artifact_path": str(r16_path),
        "r11_context_available": bool(r11),
        "r13_context_available": bool(r13),
        "r14_context_available": bool(r14),
        "r15_context_available": bool(r15),
        "r16_context_available": bool(r16),
        "cloud_target": {
            "ssh_target": target.ssh_target,
            "identity_file": target.identity_file,
            "app_path": target.app_path,
            "env_path": target.env_path,
            "db_path": target.db_path,
            "reports_path": target.reports_path,
        },
        "service_name": resolved_service_name,
        "guard_script_path": resolved_guard_path,
        "expected_existing_r5_pid": expected_r5_pid,
        "remote_probe_duration_seconds": duration,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
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


def _resolve_verification_target(
    r11: dict[str, Any],
    r13: dict[str, Any],
    r14: dict[str, Any],
    r16: dict[str, Any],
    *,
    ssh_target: str | None,
    identity_file: str | None,
    app_path: str | None,
    env_path: str | None,
    db_path: str | None,
) -> CloudBootstrapTarget:
    target = dict(
        r16.get("cloud_target")
        or r13.get("cloud_target")
        or r14.get("cloud_target")
        or {}
    )
    if target:
        context = {
            "ssh_profile": {
                "user": str(target.get("ssh_target") or "kalshi@159.65.35.72").split("@")[0],
                "host": str(target.get("ssh_target") or "kalshi@159.65.35.72").split("@")[-1],
                "identity_file": target.get("identity_file"),
            },
            "remote_paths": {
                "app_path": target.get("app_path"),
                "env_path": target.get("env_path"),
                "db_path": target.get("db_path"),
                "reports_path": target.get("reports_path"),
            },
        }
    else:
        context = r11
    return _resolve_target(
        context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    service_name: str,
    guard_script_path: str,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(service_name)
    guard = shlex.quote(guard_script_path)
    source_env = f"set -a && . {env} && set +a"
    return [
        RemoteProbe(
            "service_unit_file",
            f"test -f /etc/systemd/system/{service} && sed -n '1,220p' "
            f"/etc/systemd/system/{service}",
            timeout_seconds,
        ),
        RemoteProbe(
            "guard_script",
            f"test -x {guard} && sed -n '1,220p' {guard}",
            timeout_seconds,
        ),
        RemoteProbe(
            "systemd_unit",
            (
                f"systemctl show {service} --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "systemd_enabled",
            f"systemctl is-enabled {service} || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "systemd_active",
            f"systemctl is-active {service} || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r17_r5_status.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_guard_dry_run",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-unattended-guard "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r17_guard.out && "
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
    service_text = _stdout(by_name.get("service_unit_file"))
    guard_text = _stdout(by_name.get("guard_script"))
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
    r5_pid = _to_int(r5_status.get("pid") if isinstance(r5_status, dict) else None)
    if r5_pid is None and pids:
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
    pid_file_value = _to_int(_first_line(_stdout(by_name.get("r5_pid_file"))))
    return {
        "service_unit_file_present": bool(by_name.get("service_unit_file", None))
        and bool(by_name["service_unit_file"].ok),
        "service_unit_contains_guard": "ExecStartPre=" in service_text
        and "kalshi-r5-start-guard.sh" in service_text,
        "service_unit_runs_r5": "phase3bc-r5-crypto-freshness-watch" in service_text,
        "guard_script_present": bool(by_name.get("guard_script", None))
        and bool(by_name["guard_script"].ok),
        "guard_blocks_duplicate_r5": "Refusing duplicate R5 start" in guard_text
        or "phase3bc-r5-crypto-freshness-watch" in guard_text,
        "guard_checks_writer": "db-writer-monitor --json" in guard_text,
        "systemd_unit": systemd,
        "service_loaded": systemd.get("LoadState") == "loaded",
        "service_enabled_state": enabled or systemd.get("UnitFileState"),
        "service_enabled": (enabled or systemd.get("UnitFileState")) == "enabled",
        "service_active_state": active or systemd.get("ActiveState"),
        "service_sub_state": systemd.get("SubState"),
        "service_exec_main_pid": service_exec_main_pid,
        "service_started": (active or systemd.get("ActiveState")) == "active"
        or bool(service_exec_main_pid),
        "r5_status": r5_status,
        "guard_dry_run": guard,
        "db_writer_monitor": writer,
        "r5_running": bool((process or {}).get("phase3bc_r5_process_running")) or bool(pids),
        "r5_pids": pids,
        "r5_pid": r5_pid,
        "pid_file_value": pid_file_value,
        "duplicate_r5": len(pids) > 1,
        "guard_status": guard_status,
        "guard_should_stop": guard_should_stop,
        "watch_state": r5_status.get("latest_watch_state") if isinstance(r5_status, dict) else None,
        "paper_ready_candidates": (latest_summary or {}).get("paper_ready_candidates"),
        "positive_ev_rows": (latest_summary or {}).get("positive_ev_rows"),
        "liquidity_actionability_state": (latest_summary or {}).get(
            "liquidity_actionability_state"
        ),
        "writer_status": writer.get("status") if isinstance(writer, dict) else "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write"))
        if isinstance(writer, dict)
        else False,
        "writer_pid": writer_pid,
        "writer_matches_r5": writer_pid is None or writer_pid == r5_pid,
    }


def _verification_checks(
    *,
    r13: dict[str, Any],
    r14: dict[str, Any],
    r15: dict[str, Any],
    r16: dict[str, Any],
    parsed: dict[str, Any],
    expected_r5_pid: int | None,
) -> list[dict[str, Any]]:
    r13_decision = r13.get("adoption_decision") or {}
    service_plan = r14.get("service_plan") or {}
    install_review = r15.get("install_review_decision") or {}
    handoff = r16.get("handoff_decision") or {}
    r5_pid = _to_int(parsed.get("r5_pid"))
    return [
        _check("r16_artifact_present", bool(r16), "R16 handoff artifact exists."),
        _check(
            "r16_handoff_ready",
            handoff.get("status") == "HANDOFF_READY_ENABLE_NO_START",
            f"R16 status is {handoff.get('status')}.",
        ),
        _check(
            "r13_adopted_existing_r5",
            r13_decision.get("recommendation") == "ADOPT_EXISTING_R5",
            f"R13 recommendation is {r13_decision.get('recommendation')}.",
        ),
        _check(
            "r14_service_plan_ready",
            service_plan.get("status") == "DRAFT_READY_FOR_REVIEW",
            f"R14 status is {service_plan.get('status')}.",
        ),
        _check(
            "r15_review_ready",
            install_review.get("status") == "READY_FOR_OPERATOR_INSTALL_REVIEW_NO_START",
            f"R15 status is {install_review.get('status')}.",
        ),
        _check(
            "service_unit_installed",
            bool(parsed.get("service_unit_file_present")) and bool(parsed.get("service_loaded")),
            f"LoadState={parsed.get('systemd_unit', {}).get('LoadState')}.",
        ),
        _check(
            "service_uses_guard",
            bool(parsed.get("service_unit_contains_guard")),
            "Installed service keeps the R5 duplicate-start guard.",
        ),
        _check(
            "service_runs_r5_command",
            bool(parsed.get("service_unit_runs_r5")),
            "Installed service ExecStart runs the R5 watcher command.",
        ),
        _check(
            "guard_script_installed",
            bool(parsed.get("guard_script_present")),
            "Guard script is installed and executable.",
        ),
        _check(
            "guard_blocks_duplicate_and_checks_writer",
            bool(parsed.get("guard_blocks_duplicate_r5"))
            and bool(parsed.get("guard_checks_writer")),
            "Guard script blocks duplicate R5 starts and checks db-writer-monitor.",
        ),
        _check(
            "service_enabled_no_start",
            bool(parsed.get("service_enabled")),
            f"Service enabled state is {parsed.get('service_enabled_state')}.",
        ),
        _check(
            "service_not_started_now",
            not bool(parsed.get("service_started")),
            (
                f"ActiveState={parsed.get('service_active_state')}, "
                f"ExecMainPID={parsed.get('service_exec_main_pid')}."
            ),
        ),
        _check(
            "exactly_one_existing_r5",
            bool(parsed.get("r5_running")) and not bool(parsed.get("duplicate_r5")),
            f"R5 PIDs: {parsed.get('r5_pids')}.",
        ),
        _check(
            "r5_pid_matches_expected",
            expected_r5_pid is None or r5_pid == expected_r5_pid,
            f"Expected PID={expected_r5_pid}; current PID={r5_pid}.",
        ),
        _check(
            "r5_guard_healthy",
            parsed.get("guard_status") == "RUNNING"
            and parsed.get("guard_should_stop") is False,
            (
                f"guard_status={parsed.get('guard_status')}, "
                f"guard_should_stop={parsed.get('guard_should_stop')}."
            ),
        ),
        _check(
            "writer_not_conflicting",
            bool(parsed.get("writer_matches_r5")),
            f"writer_pid={parsed.get('writer_pid')}; r5_pid={r5_pid}.",
        ),
    ]


def _verification_decision(
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
    *,
    expected_r5_pid: int | None,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        status = "BLOCKED_SERVICE_INSTALL_VERIFICATION"
        reason = f"First failing check: {failed[0]['check']}."
        operator_command, next_codex_step = _blocked_next_step(failed, parsed)
    else:
        status = "VERIFIED_ENABLE_NO_START_HANDOFF"
        reason = (
            "The service and guard are installed, systemd is enabled without starting "
            "a duplicate watcher, and the existing R5 watcher remains healthy."
        )
        operator_command = (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        )
        next_codex_step = "Phase 3BB-R18 - Cloud Scheduler Runtime Cutover Monitor"
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "service_enabled": bool(parsed.get("service_enabled")),
        "service_started": bool(parsed.get("service_started")),
        "service_active_state": parsed.get("service_active_state"),
        "current_r5_pid": parsed.get("r5_pid"),
        "expected_existing_r5_pid": expected_r5_pid,
        "duplicate_r5": bool(parsed.get("duplicate_r5")),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "writer_pid": parsed.get("writer_pid"),
        "writer_matches_r5": bool(parsed.get("writer_matches_r5")),
        "codex_executed_install": False,
        "codex_executed_enable": False,
        "codex_executed_start": False,
        "start_allowed_now": False,
        "stop_existing_r5_allowed_now": False,
        "operator_next_command": operator_command,
        "next_codex_step": next_codex_step,
    }


def _blocked_next_step(
    failed: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> tuple[str, str]:
    failed_names = {str(row["check"]) for row in failed}
    if parsed.get("guard_should_stop") or parsed.get("guard_status") == "OVERRUNNING":
        return (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports",
            "Phase 3BB-R13 - Cloud Scheduler Adoption Recheck / Guarded Stop Decision",
        )
    install_missing_checks = {
        "service_unit_installed",
        "service_uses_guard",
        "service_runs_r5_command",
        "guard_script_installed",
        "guard_blocks_duplicate_and_checks_writer",
        "service_enabled_no_start",
    }
    if failed_names & install_missing_checks:
        return (
            "bash reports/phase3bb_r16/operator_install_handoff.sh",
            "Phase 3BB-R16 - Operator Handoff Execution Required, then rerun R17",
        )
    return (
        "kalshi-bot phase3bb-r17-cloud-service-install-verification "
        "--output-dir reports/phase3bb_r17 --reports-dir reports",
        "Phase 3BB-R17 - Resolve First Failed Verification Check",
    )


def _expected_r5_pid(r13: dict[str, Any], r16: dict[str, Any]) -> int | None:
    r16_decision = r16.get("handoff_decision") or {}
    r13_decision = r13.get("adoption_decision") or {}
    return _to_int(r16_decision.get("current_r5_pid")) or _to_int(
        r13_decision.get("current_r5_pid")
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
    lines = _metadata_lines(payload, "# Phase 3BB-R17 Cloud Service Install Verification")
    decision = payload["verification_decision"]
    parsed = payload["parsed_remote_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Cloud Service",
            "",
            f"- Service: `{payload['service_name']}`",
            f"- Installed: `{parsed.get('service_unit_file_present')}`",
            f"- Enabled: `{decision['service_enabled']}`",
            f"- Started now: `{decision['service_started']}`",
            f"- Active state: `{decision['service_active_state']}`",
            f"- Guard installed: `{parsed.get('guard_script_present')}`",
            "",
            "## R5 Watcher",
            "",
            f"- Expected PID: `{decision['expected_existing_r5_pid']}`",
            f"- Current PID: `{decision['current_r5_pid']}`",
            f"- Duplicate R5: `{decision['duplicate_r5']}`",
            f"- Guard status: `{decision['guard_status']}`",
            f"- Guard should stop: `{decision['guard_should_stop']}`",
            f"- Watch state: `{parsed.get('watch_state')}`",
            f"- Positive EV rows: `{parsed.get('positive_ev_rows')}`",
            f"- Paper-ready candidates: `{parsed.get('paper_ready_candidates')}`",
            "",
            "## Safety",
            "",
            "- Codex did not install, enable, or start the service.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R17 Install Verification Detail")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Verification Scope",
            "",
            "This phase verifies the operator-run R16 install+enable-no-start handoff. "
            "It only runs bounded read-only SSH/systemd/status probes and writes local "
            "reports.",
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
    for row in payload["verification_checks"]:
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
    command = payload["verification_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R17 next safe status command.",
            command,
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R17 Next Actions")
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
            "- Do not run `systemctl start` while an existing R5 watcher is active.",
            "- Do not stop the existing R5 watcher from this verification phase.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    if decision["verification_passed"]:
        lines.extend(
            [
                "",
                "## Verified State",
                "",
                "- The cloud service is installed and enabled for future startup.",
                "- The service is not started now, avoiding duplicate R5 ownership.",
                "- Exactly one existing guarded R5 watcher remains the active writer.",
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


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
