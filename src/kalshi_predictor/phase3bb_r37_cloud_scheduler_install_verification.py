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
    _json_from_probe,
    _loose_db_writer_state,
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

PHASE3BB_R37_VERSION = "phase3bb_r37_cloud_scheduler_install_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r37")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
READY_R36_STATUS = "HANDOFF_READY_SCHEDULER_INSTALL_ENABLE_NO_START"

FORBIDDEN_RUNNER_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "autopilot-run",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "phase3bc-r5-unattended-start",
    "place-order",
    "replace-order",
    "submit-order",
    "systemctl start",
    "systemctl restart",
    "systemctl enable --now",
)


@dataclass(frozen=True)
class Phase3BBR37CloudSchedulerInstallVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r37_cloud_scheduler_install_verification_report(
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
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR37CloudSchedulerInstallVerificationArtifacts:
    payload = build_phase3bb_r37_cloud_scheduler_install_verification(
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
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_install_verification.md"
    json_path = output_dir / "cloud_scheduler_install_verification.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "verification_checks.csv"
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
    return Phase3BBR37CloudSchedulerInstallVerificationArtifacts(
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


def build_phase3bb_r37_cloud_scheduler_install_verification(
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
    scheduler_service_name: str = SCHEDULER_SERVICE_NAME,
    scheduler_timer_name: str = SCHEDULER_TIMER_NAME,
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
        "command": "kalshi-bot phase3bb-r37-cloud-scheduler-install-verification",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r36_path = reports_dir / "phase3bb_r36" / "cloud_scheduler_install_handoff.json"
    r11 = _read_json(r11_path)
    r36 = _read_json(r36_path)
    target = _resolve_target(
        r11,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    probes = _build_remote_probes(
        target,
        scheduler_service_name=scheduler_service_name,
        scheduler_timer_name=scheduler_timer_name,
        timeout_seconds=per_probe_timeout_seconds,
    )
    runner = probe_runner or _run_ssh_probe
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results)
    checks = _verification_checks(r11=r11, r36=r36, parsed=parsed)
    decision = _verification_decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "post_operator_verification_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "systemctl_read_only_commands_executed": 5,
        "systemctl_mutating_commands_executed": 0,
        "remote_report_writes_performed": 2,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "scheduler_files_written_to_system": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_refresh_jobs": False,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R37-CLOUD-SCHEDULER-INSTALL-VERIFICATION",
        "phase_version": PHASE3BB_R37_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_INSTALL_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r36_artifact_path": str(r36_path),
        "r11_context_available": bool(r11),
        "r36_context_available": bool(r36),
        "cloud_target": _target_payload(target),
        "scheduler_service_name": scheduler_service_name,
        "scheduler_timer_name": scheduler_timer_name,
        "r36_handoff_decision": r36.get("handoff_decision") or {},
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


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    scheduler_service_name: str,
    scheduler_timer_name: str,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    service = shlex.quote(scheduler_service_name)
    timer = shlex.quote(scheduler_timer_name)
    runner_path = shlex.quote(f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}")
    source_env = f"set -a && . {env} && set +a"
    registry_commands = [
        "db-writer-monitor",
        "phase3bb-r2-weather-fast-lane",
        "phase3bb-r8-unified-paper-gate",
        "phase3bb-r33-cloud-paper-only-operations-readiness",
        "phase3bb-r34-cloud-multicategory-refresh-scheduler-review",
        "phase3bc-r5-status",
    ]
    registry_loop = " ".join(shlex.quote(command) for command in registry_commands)
    return [
        RemoteProbe(
            "scheduler_service_unit_file",
            f"test -f /etc/systemd/system/{service} && sed -n '1,220p' "
            f"/etc/systemd/system/{service}",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_timer_unit_file",
            f"test -f /etc/systemd/system/{timer} && sed -n '1,220p' "
            f"/etc/systemd/system/{timer}",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_runner_script",
            f"test -x {runner_path} && sed -n '1,260p' {runner_path}",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_service_systemd",
            (
                f"systemctl show {service} --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_timer_systemd",
            (
                f"systemctl show {timer} --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe("scheduler_timer_enabled", f"systemctl is-enabled {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r37_r5_status.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_guard_dry_run",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-unattended-guard "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r37_guard.out && "
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
            "command_registry",
            (
                f"cd {app} && for cmd in {registry_loop}; do "
                ".venv/bin/kalshi-bot \"$cmd\" --help >/dev/null || exit 30; "
                "done; echo COMMAND_REGISTRY_OK"
            ),
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    service_text = _stdout(by_name.get("scheduler_service_unit_file"))
    timer_text = _stdout(by_name.get("scheduler_timer_unit_file"))
    runner_text = _stdout(by_name.get("scheduler_runner_script"))
    service_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_service_systemd")))
    timer_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_timer_systemd")))
    command_registry_result = by_name.get("command_registry")
    timer_enabled = _first_line(_stdout(by_name.get("scheduler_timer_enabled")))
    timer_active = _first_line(_stdout(by_name.get("scheduler_timer_active")))
    service_active = _first_line(_stdout(by_name.get("scheduler_service_active")))
    r5_status = _json_from_probe(by_name.get("r5_status"))
    guard = _json_from_probe(by_name.get("r5_guard_dry_run"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    status_guard = r5_status.get("guard") if isinstance(r5_status, dict) else {}
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
    guard_status = (
        (status_guard or {}).get("status")
        or (guard_after_guard or {}).get("status")
        or guard.get("status")
    )
    guard_should_stop = bool(
        (status_guard or {}).get("should_stop")
        or (guard_after_guard or {}).get("should_stop")
        or guard.get("should_stop")
    )
    writer_pid = _to_int(writer.get("current_writer_pid") if isinstance(writer, dict) else None)
    forbidden_runner_hits = [
        fragment for fragment in FORBIDDEN_RUNNER_FRAGMENTS if fragment in runner_text.lower()
    ]
    service_exec_main_pid = _to_int(service_systemd.get("ExecMainPID"))
    timer_exec_main_pid = _to_int(timer_systemd.get("ExecMainPID"))
    service_active_state = service_active or service_systemd.get("ActiveState")
    timer_active_state = timer_active or timer_systemd.get("ActiveState")
    return {
        "scheduler_service_unit_file_present": bool(
            by_name.get("scheduler_service_unit_file")
            and by_name["scheduler_service_unit_file"].ok
        ),
        "scheduler_timer_unit_file_present": bool(
            by_name.get("scheduler_timer_unit_file") and by_name["scheduler_timer_unit_file"].ok
        ),
        "scheduler_runner_script_present": bool(
            by_name.get("scheduler_runner_script") and by_name["scheduler_runner_script"].ok
        ),
        "scheduler_service_unit_text_contains_runner": RUNNER_SCRIPT_NAME in service_text,
        "scheduler_service_unit_is_oneshot": "Type=oneshot" in service_text,
        "scheduler_timer_unit_text_contains_schedule": "[Timer]" in timer_text
        and ("OnCalendar=" in timer_text or "OnUnitActiveSec=" in timer_text),
        "scheduler_runner_has_writer_gate": "db-writer-monitor --json" in runner_text
        and "writer active; skip writer-gated job" in runner_text.lower(),
        "scheduler_runner_checks_r5_status": "phase3bc-r5-status" in runner_text,
        "scheduler_runner_has_forbidden_fragments": bool(forbidden_runner_hits),
        "scheduler_runner_forbidden_fragments": forbidden_runner_hits,
        "scheduler_service_systemd": service_systemd,
        "scheduler_timer_systemd": timer_systemd,
        "scheduler_service_loaded": service_systemd.get("LoadState") == "loaded",
        "scheduler_timer_loaded": timer_systemd.get("LoadState") == "loaded",
        "scheduler_timer_enabled_state": timer_enabled or timer_systemd.get("UnitFileState"),
        "scheduler_timer_enabled": (timer_enabled or timer_systemd.get("UnitFileState"))
        == "enabled",
        "scheduler_service_active_state": service_active_state,
        "scheduler_timer_active_state": timer_active_state,
        "scheduler_service_started": service_active_state in {"active", "activating"}
        or (
            service_active_state not in {"", "failed", "inactive"}
            and bool(service_exec_main_pid)
        ),
        "scheduler_timer_started": timer_active_state == "active"
        or bool(timer_exec_main_pid),
        "command_registry_ok": bool(
            by_name.get("command_registry") and by_name["command_registry"].ok
        ),
        "command_registry_missing_command": _missing_cli_command(
            command_registry_result.stderr if command_registry_result else ""
        ),
        "r5_status": r5_status,
        "r5_guard_dry_run": guard,
        "db_writer_monitor": writer,
        "r5_running": bool((process or {}).get("phase3bc_r5_process_running")) or bool(pids),
        "r5_pid": r5_pid,
        "r5_pids": pids,
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
        "writer_matches_r5_or_clear": writer_pid is None or writer_pid == r5_pid,
    }


def _verification_checks(
    *,
    r11: dict[str, Any],
    r36: dict[str, Any],
    parsed: dict[str, Any],
) -> list[dict[str, Any]]:
    r36_decision = r36.get("handoff_decision") or {}
    return [
        _check("r11_cloud_context_present", bool(r11), "R11 cloud context exists."),
        _check("r36_artifact_present", bool(r36), "R36 handoff artifact exists."),
        _check(
            "r36_handoff_ready",
            r36_decision.get("status") == READY_R36_STATUS
            and bool(r36_decision.get("handoff_ready")),
            f"R36 status is {r36_decision.get('status')}.",
        ),
        _check(
            "scheduler_service_unit_installed",
            bool(parsed.get("scheduler_service_unit_file_present"))
            and bool(parsed.get("scheduler_service_loaded")),
            f"Service LoadState={parsed.get('scheduler_service_systemd', {}).get('LoadState')}.",
        ),
        _check(
            "scheduler_timer_unit_installed",
            bool(parsed.get("scheduler_timer_unit_file_present"))
            and bool(parsed.get("scheduler_timer_loaded")),
            f"Timer LoadState={parsed.get('scheduler_timer_systemd', {}).get('LoadState')}.",
        ),
        _check(
            "scheduler_runner_installed",
            bool(parsed.get("scheduler_runner_script_present")),
            "Runner script exists and is executable.",
        ),
        _check(
            "scheduler_service_runs_runner",
            bool(parsed.get("scheduler_service_unit_text_contains_runner"))
            and bool(parsed.get("scheduler_service_unit_is_oneshot")),
            "Scheduler service runs the reviewed oneshot runner.",
        ),
        _check(
            "scheduler_timer_has_schedule",
            bool(parsed.get("scheduler_timer_unit_text_contains_schedule")),
            "Scheduler timer has a Timer section and a bounded schedule.",
        ),
        _check(
            "scheduler_runner_has_writer_gate",
            bool(parsed.get("scheduler_runner_has_writer_gate")),
            "Runner checks db-writer-monitor before writer-gated jobs.",
        ),
        _check(
            "scheduler_runner_checks_r5",
            bool(parsed.get("scheduler_runner_checks_r5_status")),
            "Runner observes R5 status before status/report work.",
        ),
        _check(
            "scheduler_runner_has_no_forbidden_commands",
            not bool(parsed.get("scheduler_runner_has_forbidden_fragments")),
            (
                "Forbidden hits: "
                f"{', '.join(parsed.get('scheduler_runner_forbidden_fragments') or []) or 'none'}."
            ),
        ),
        _check(
            "scheduler_timer_enabled_no_start",
            bool(parsed.get("scheduler_timer_enabled")),
            f"Timer enabled state is {parsed.get('scheduler_timer_enabled_state')}.",
        ),
        _check(
            "scheduler_timer_runtime_state_valid",
            parsed.get("scheduler_timer_active_state") in {"active", "inactive"},
            f"Timer active state is {parsed.get('scheduler_timer_active_state')}.",
        ),
        _check(
            "scheduler_service_runtime_state_valid",
            parsed.get("scheduler_service_active_state") in {"active", "activating", "inactive"},
            f"Service active state is {parsed.get('scheduler_service_active_state')}.",
        ),
        _check(
            "remote_command_registry_ok",
            bool(parsed.get("command_registry_ok")),
            (
                f"Missing command: {parsed.get('command_registry_missing_command')}."
                if parsed.get("command_registry_missing_command")
                else "Runner/status commands are registered on the cloud host."
            ),
        ),
        _check(
            "exactly_one_r5_running",
            bool(parsed.get("r5_running")) and not bool(parsed.get("duplicate_r5")),
            f"R5 PIDs: {parsed.get('r5_pids')}.",
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
            bool(parsed.get("writer_matches_r5_or_clear")),
            (
                f"writer_pid={parsed.get('writer_pid')}; "
                f"r5_pid={parsed.get('r5_pid')}."
            ),
        ),
    ]


def _verification_decision(
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        status = "BLOCKED_SCHEDULER_INSTALL_VERIFICATION"
        reason = f"First failing check: {failed[0]['check']}."
        operator_command, next_codex_step = _blocked_next_step(failed, parsed)
    else:
        timer_started = bool(parsed.get("scheduler_timer_started"))
        if timer_started:
            status = "VERIFIED_SCHEDULER_INSTALL_TIMER_ACTIVE"
            reason = (
                "The cloud scheduler service, timer, and runner are installed; the timer is "
                "enabled and active; and exactly one healthy R5 watcher remains."
            )
            operator_command = (
                "systemctl status kalshi-multicategory-refresh-scheduler.timer "
                "kalshi-multicategory-refresh-scheduler.service --no-pager"
            )
            next_codex_step = "Phase 3BB-R40 - Cloud Scheduler Runtime Monitor"
        else:
            status = "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START"
            reason = (
                "The cloud scheduler service, timer, and runner are installed; the timer is "
                "enabled without being started; and exactly one healthy R5 watcher remains."
            )
            operator_command = (
                "kalshi-bot phase3bb-r38-cloud-scheduler-timer-start-handoff "
                "--output-dir reports/phase3bb_r38 --reports-dir reports"
            )
            next_codex_step = "Phase 3BB-R38 - Operator-Approved Cloud Scheduler Timer Start Handoff"
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "scheduler_service_installed": bool(parsed.get("scheduler_service_loaded")),
        "scheduler_timer_installed": bool(parsed.get("scheduler_timer_loaded")),
        "scheduler_timer_enabled": bool(parsed.get("scheduler_timer_enabled")),
        "scheduler_timer_started": bool(parsed.get("scheduler_timer_started")),
        "scheduler_service_started": bool(parsed.get("scheduler_service_started")),
        "current_r5_pid": parsed.get("r5_pid"),
        "duplicate_r5": bool(parsed.get("duplicate_r5")),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "watch_state": parsed.get("watch_state"),
        "positive_ev_rows": parsed.get("positive_ev_rows"),
        "paper_ready_candidates": parsed.get("paper_ready_candidates"),
        "writer_pid": parsed.get("writer_pid"),
        "writer_matches_r5_or_clear": bool(parsed.get("writer_matches_r5_or_clear")),
        "command_registry_missing_command": parsed.get("command_registry_missing_command"),
        "codex_executed_install": False,
        "codex_executed_enable": False,
        "codex_executed_start": False,
        "codex_ran_scheduler_jobs": False,
        "ready_for_timer_start_handoff": not failed
        and not bool(parsed.get("scheduler_timer_started")),
        "ready_for_runtime_monitor": not failed and bool(parsed.get("scheduler_timer_started")),
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
    install_checks = {
        "scheduler_service_unit_installed",
        "scheduler_timer_unit_installed",
        "scheduler_runner_installed",
        "scheduler_timer_enabled_no_start",
    }
    if failed_names & install_checks:
        return (
            "PHASE3BB_R36_EXECUTE=I_APPROVE_R36_SCHEDULER_INSTALL "
            "bash reports/phase3bb_r36/operator_scheduler_install_handoff.sh",
            "Phase 3BB-R36 - Run Operator Scheduler Install Handoff, then rerun R37",
        )
    if failed_names & {
        "scheduler_timer_runtime_state_valid",
        "scheduler_service_runtime_state_valid",
    }:
        return (
            "systemctl status kalshi-multicategory-refresh-scheduler.timer "
            "kalshi-multicategory-refresh-scheduler.service --no-pager",
            "Phase 3BB-R37 - Review Unexpected Scheduler Runtime State",
        )
    return (
        "kalshi-bot phase3bb-r37-cloud-scheduler-install-verification "
        "--output-dir reports/phase3bb_r37 --reports-dir reports",
        "Phase 3BB-R37 - Resolve First Failed Verification Check",
    )


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


def _missing_cli_command(stderr: str) -> str | None:
    marker = "No such command '"
    start = stderr.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = stderr.find("'", start)
    if end < 0:
        return None
    return stderr[start:end].strip() or None


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R37 Cloud Scheduler Install Verification")
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
            "## Scheduler",
            "",
            f"- Service: `{payload['scheduler_service_name']}`",
            f"- Service installed: `{decision['scheduler_service_installed']}`",
            f"- Service started: `{decision['scheduler_service_started']}`",
            f"- Timer: `{payload['scheduler_timer_name']}`",
            f"- Timer installed: `{decision['scheduler_timer_installed']}`",
            f"- Timer enabled: `{decision['scheduler_timer_enabled']}`",
            f"- Timer started: `{decision['scheduler_timer_started']}`",
            f"- Timer active state: `{parsed.get('scheduler_timer_active_state')}`",
            "",
            "## R5 Watcher",
            "",
            f"- Current PID: `{decision['current_r5_pid']}`",
            f"- Duplicate R5: `{decision['duplicate_r5']}`",
            f"- Guard status: `{decision['guard_status']}`",
            f"- Guard should stop: `{decision['guard_should_stop']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Positive EV rows: `{decision['positive_ev_rows']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            "",
            "## Safety",
            "",
            "- Codex did not install, enable, start, or stop any cloud service.",
            "- Codex did not run multi-category refresh jobs.",
            "- Codex did not stop or duplicate R5.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R37 Scheduler Install Verification Detail")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Verification Scope",
            "",
            "This phase verifies the operator-run R36 scheduler install handoff. It only "
            "runs bounded read-only SSH/systemd/status probes, then writes local reports.",
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
            "# Phase 3BB-R37 next safe operator command.",
            command,
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R37 Next Actions")
    decision = payload["verification_decision"]
    timer_state_line = (
        "- The scheduler timer is enabled and active."
        if decision["scheduler_timer_started"]
        else "- The scheduler timer is enabled but not started."
    )
    service_state_line = (
        "- The scheduler service is currently running a scheduled cycle."
        if decision["scheduler_service_started"]
        else "- The scheduler service is inactive between timer ticks."
    )
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
            "- Do not start the scheduler timer outside an approved handoff.",
            "- Do not start duplicate R5 watchers.",
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
                "- The scheduler service, timer, and runner are installed.",
                timer_state_line,
                service_state_line,
                "- Exactly one guarded R5 watcher remains active.",
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
