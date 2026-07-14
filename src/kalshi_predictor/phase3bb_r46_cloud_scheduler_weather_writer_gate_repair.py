from __future__ import annotations

import base64
import csv
import json
import re
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
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    RUNNER_SCRIPT_NAME,
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.phase3bb_r44_weather_catalog_hook_runtime_verification import (
    _first_line,
    _mark_executable,
    _stdout,
    _target_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R46_VERSION = "phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r46")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45

RUN_JOB_BLOCK = "\n".join(
    [
        "run_job() {",
        "  local job_id=\"$1\"",
        "  local writer_capable=\"$2\"",
        "  shift 2",
        "  if [[ \"${writer_capable}\" == \"true\" ]] && ! writer_clear; then",
        "    echo \"[phase3bb-r35] Writer active; skip writer-gated job ${job_id}\"",
        "    return 0",
        "  fi",
        "  echo \"[phase3bb-r35] running ${job_id}\"",
        "  local output status",
        "  set +e",
        "  output=$(\"$@\" 2>&1)",
        "  status=$?",
        "  set -e",
        "  if [[ -n \"${output}\" ]]; then",
        "    printf '%s\\n' \"${output}\"",
        "  fi",
        "  if [[ \"${status}\" -ne 0 ]]; then",
        "    if [[ \"${writer_capable}\" == \"true\" ]] && printf '%s\\n' \"${output}\" | grep -Eq 'Status: BUSY_WRITER|Database is busy|safe_to_start_write[^A-Za-z0-9_:-]*false'; then",
        "      echo \"[phase3bb-r35] Writer became active during ${job_id}; clean skip for retry\"",
        "      return 0",
        "    fi",
        "    return \"${status}\"",
        "  fi",
        "}",
    ]
)

FORBIDDEN_REPAIR_FRAGMENTS = (
    "accelerate-learning",
    "autopilot-once",
    "cancel-order",
    "create-paper-trade",
    "demo-order",
    "live-order",
    "paper-trade-create",
    "place-order",
    "replace-order",
    "submit-order",
    "systemctl start",
    "systemctl restart",
    "systemctl enable --now",
)


@dataclass(frozen=True)
class Phase3BBR46CloudSchedulerWeatherWriterGateRepairArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    runner_patch_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair_report(
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
    apply: bool = False,
    backup_first: bool = False,
    reset_failed: bool = False,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR46CloudSchedulerWeatherWriterGateRepairArtifacts:
    payload = build_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair(
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
        apply=apply,
        backup_first=backup_first,
        reset_failed=reset_failed,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_weather_writer_gate_repair.md"
    json_path = output_dir / "cloud_scheduler_weather_writer_gate_repair.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "repair_checks.csv"
    runner_patch_path = output_dir / f"{RUNNER_SCRIPT_NAME}.phase3bb_r46.patch"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_rows_csv(probe_csv_path, payload["remote_probe_results"])
    _write_rows_csv(checks_csv_path, payload["repair_checks"])
    runner_patch_path.write_text(payload["patched_runner_script"], encoding="utf-8")
    _mark_executable(runner_patch_path)
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
            runner_patch_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR46CloudSchedulerWeatherWriterGateRepairArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        runner_patch_path=runner_patch_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r46_cloud_scheduler_weather_writer_gate_repair(
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
    apply: bool = False,
    backup_first: bool = False,
    reset_failed: bool = False,
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
        "command": "kalshi-bot phase3bb-r46-cloud-scheduler-weather-writer-gate-repair",
        "argv": command_args or [],
    }
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    r45 = _read_json(reports_dir / "phase3bb_r45" / "weather_freshness_to_ranking_impact.json")
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(target, timeout_seconds=per_probe_timeout_seconds)
    results = [runner(probe, target) for probe in probes]
    parsed = _parse_probe_outputs(results, r45=r45, target=target)
    patched_runner = patch_runner_midrun_writer_gate(parsed.get("runner_script") or "")
    parsed["patched_runner_has_midrun_writer_gate"] = _runner_has_midrun_writer_gate(patched_runner)
    parsed["runner_patch_required"] = bool(
        patched_runner and patched_runner != (parsed.get("runner_script") or "")
    )
    checks = _repair_checks(
        parsed=parsed,
        patched_runner=patched_runner,
        apply=apply,
        backup_first=backup_first,
    )
    install_result: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }
    verify_after = parsed.get("runner_has_midrun_writer_gate", False)
    service_result_after = parsed.get("scheduler_service_result")
    if apply and _can_apply(checks, parsed):
        install_probe = _build_install_probe(
            target,
            patched_runner=patched_runner,
            backup_first=backup_first,
            reset_failed=reset_failed,
            timeout_seconds=per_probe_timeout_seconds,
        )
        install_probe_result = runner(install_probe, target)
        results.append(install_probe_result)
        install_result = {
            "attempted": True,
            "ok": bool(install_probe_result.ok),
            "exit_code": install_probe_result.exit_code,
            "stdout": install_probe_result.stdout[-4000:],
            "stderr": install_probe_result.stderr[-4000:],
        }
        verify_probes = _build_post_apply_probes(target, timeout_seconds=per_probe_timeout_seconds)
        verify_results = [runner(probe, target) for probe in verify_probes]
        results.extend(verify_results)
        verify = _parse_probe_outputs(verify_results, r45=r45, target=target)
        verify_after = bool(verify.get("runner_has_midrun_writer_gate"))
        service_result_after = verify.get("scheduler_service_result")
    decision = _decision(
        checks=checks,
        parsed=parsed,
        apply=apply,
        backup_first=backup_first,
        reset_failed=reset_failed,
        install_result=install_result,
        verify_after=bool(verify_after),
        service_result_after=service_result_after,
    )
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": not apply,
        "scheduler_weather_writer_gate_repair": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 1 if install_result["attempted"] else 0,
        "systemctl_reset_failed_executed": bool(apply and reset_failed and install_result["attempted"]),
        "systemctl_start_stop_restart_executed": 0,
        "scheduler_runner_written_to_system": bool(install_result["attempted"] and install_result["ok"]),
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
        "scheduler_service_stopped": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "runs_weather_fast_lane": False,
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
        "phase": "3BB-R46-CLOUD-SCHEDULER-WEATHER-WRITER-GATE-REPAIR",
        "phase_version": PHASE3BB_R46_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_WEATHER_WRITER_GATE_REPAIR",
        "reports_dir": str(reports_dir),
        "cloud_target": _target_payload(target),
        "apply_requested": apply,
        "backup_first": backup_first,
        "reset_failed": reset_failed,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_repair_state": parsed,
        "repair_checks": checks,
        "install_result": install_result,
        "repair_decision": decision,
        "current_runner_script": parsed.get("runner_script") or "",
        "patched_runner_script": patched_runner,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_remote_probes(target: CloudBootstrapTarget, *, timeout_seconds: int) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    runner_path = shlex.quote(_runner_path(target))
    writer_cmd = f"cd {app} && set -a && . {env} && set +a && .venv/bin/kalshi-bot db-writer-monitor --json"
    registry_loop = " ".join(
        shlex.quote(command)
        for command in (
            "phase3bb-r40-cloud-scheduler-runtime-monitor",
            "phase3bb-r45-weather-freshness-to-ranking-impact",
            "phase3az-r12-weather-activation-preview",
            "phase3bb-r2-weather-fast-lane",
        )
    )
    return [
        RemoteProbe("remote_time_utc", "date -u +%Y-%m-%dT%H:%M:%SZ", timeout_seconds),
        RemoteProbe("runner_script", f"test -x {runner_path} && sed -n '1,320p' {runner_path}", timeout_seconds),
        RemoteProbe(
            "scheduler_service_systemd",
            f"systemctl show {SCHEDULER_SERVICE_NAME} --property=LoadState,ActiveState,SubState,ExecMainPID,Result,NRestarts || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_timer_systemd",
            f"systemctl show {SCHEDULER_TIMER_NAME} --property=LoadState,ActiveState,SubState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime || true",
            timeout_seconds,
        ),
        RemoteProbe("scheduler_journal_tail", f"journalctl -u {SCHEDULER_SERVICE_NAME} -n 160 --no-pager || true", timeout_seconds),
        RemoteProbe("db_writer_monitor_raw", writer_cmd, timeout_seconds),
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


def _build_post_apply_probes(target: CloudBootstrapTarget, *, timeout_seconds: int) -> list[RemoteProbe]:
    runner_path = shlex.quote(_runner_path(target))
    return [
        RemoteProbe("runner_script", f"test -x {runner_path} && sed -n '1,320p' {runner_path}", timeout_seconds),
        RemoteProbe(
            "scheduler_service_systemd",
            f"systemctl show {SCHEDULER_SERVICE_NAME} --property=LoadState,ActiveState,SubState,ExecMainPID,Result,NRestarts || true",
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(
    results: list[RemoteProbeResult],
    *,
    r45: dict[str, Any],
    target: CloudBootstrapTarget,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    runner_script = _stdout(by_name.get("runner_script"))
    service_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_service_systemd")))
    timer_systemd = _parse_systemd_show(_stdout(by_name.get("scheduler_timer_systemd")))
    writer = _json_from_probe(by_name.get("db_writer_monitor_raw"))
    if not isinstance(writer, dict):
        writer = {}
    journal = _stdout(by_name.get("scheduler_journal_tail"))
    r45_decision = r45.get("impact_decision") if isinstance(r45, dict) else {}
    if not isinstance(r45_decision, dict):
        r45_decision = {}
    return {
        "remote_time_utc": _first_line(_stdout(by_name.get("remote_time_utc"))),
        "runner_path": _runner_path(target),
        "runner_script": runner_script,
        "runner_exists": bool(runner_script.strip()),
        "runner_has_midrun_writer_gate": _runner_has_midrun_writer_gate(runner_script),
        "runner_has_weather_catalog_hook": "weather_current_catalog_refresh" in runner_script,
        "runner_has_weather_fast_lane": "weather_fast_lane" in runner_script,
        "scheduler_service_systemd": service_systemd,
        "scheduler_timer_systemd": timer_systemd,
        "scheduler_service_active_state": service_systemd.get("ActiveState"),
        "scheduler_service_result": service_systemd.get("Result"),
        "scheduler_timer_active_state": timer_systemd.get("ActiveState"),
        "scheduler_timer_unit_file_state": timer_systemd.get("UnitFileState"),
        "writer_status": writer.get("status") or "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write")) if writer else False,
        "writer_pid": writer.get("current_writer_pid"),
        "journal_busy_writer_seen": "Status: BUSY_WRITER" in journal or "Database is busy" in journal,
        "journal_clean_midrun_skip_seen": "Writer became active during" in journal,
        "r45_status": r45_decision.get("status"),
        "r45_first_weather_blocker": r45_decision.get("first_weather_blocker"),
        "command_registry_ok": bool(by_name.get("command_registry") and by_name["command_registry"].ok),
    }


def patch_runner_midrun_writer_gate(runner_script: str) -> str:
    if not runner_script.strip() or _runner_has_midrun_writer_gate(runner_script):
        return runner_script
    pattern = re.compile(r"(?ms)^run_job\(\) \{\n.*?^\}")
    patched, count = pattern.subn(RUN_JOB_BLOCK, runner_script, count=1)
    return patched if count else runner_script


def _runner_has_midrun_writer_gate(runner_script: str) -> bool:
    return (
        "Writer became active during" in runner_script
        and "Status: BUSY_WRITER|Database is busy" in runner_script
        and "output=$(\"$@\" 2>&1)" in runner_script
    )


def _repair_checks(
    *,
    parsed: dict[str, Any],
    patched_runner: str,
    apply: bool,
    backup_first: bool,
) -> list[dict[str, Any]]:
    service_state = parsed.get("scheduler_service_active_state")
    forbidden_hits = sorted(
        {fragment for fragment in FORBIDDEN_REPAIR_FRAGMENTS if fragment in patched_runner.lower()}
    )
    return [
        _check("runner_script_found", bool(parsed.get("runner_exists")), f"runner={parsed.get('runner_path')}."),
        _check(
            "runner_has_weather_catalog_hook",
            bool(parsed.get("runner_has_weather_catalog_hook")),
            f"weather_current_catalog_refresh={parsed.get('runner_has_weather_catalog_hook')}.",
        ),
        _check(
            "runner_patchable_or_already_repaired",
            bool(patched_runner.strip()) and _runner_has_midrun_writer_gate(patched_runner),
            f"already_repaired={parsed.get('runner_has_midrun_writer_gate')} patch_required={parsed.get('runner_patch_required')}.",
        ),
        _check(
            "scheduler_service_not_running_for_apply",
            (not apply) or service_state not in {"active", "activating"},
            f"scheduler_service={service_state}.",
        ),
        _check("backup_first_for_apply", (not apply) or backup_first, f"apply={apply} backup_first={backup_first}."),
        _check(
            "no_forbidden_runner_fragments",
            not forbidden_hits,
            f"forbidden={','.join(forbidden_hits) if forbidden_hits else 'none'}.",
        ),
        _check(
            "command_registry_ok",
            bool(parsed.get("command_registry_ok")),
            "R40/R45/R12/R2 commands are registered on the cloud host.",
        ),
    ]


def _decision(
    *,
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
    apply: bool,
    backup_first: bool,
    reset_failed: bool,
    install_result: dict[str, Any],
    verify_after: bool,
    service_result_after: str | None,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if failed:
        status = "BLOCKED_SCHEDULER_WEATHER_WRITER_GATE_REPAIR"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R46 - Resolve Scheduler Writer-Gate Repair Preconditions"
        command = (
            "kalshi-bot phase3bb-r46-cloud-scheduler-weather-writer-gate-repair "
            "--output-dir reports/phase3bb_r46 --reports-dir reports"
        )
    elif parsed.get("runner_has_midrun_writer_gate") and not apply:
        status = "SCHEDULER_WRITER_GATE_ALREADY_REPAIRED"
        reason = "The installed runner already treats mid-run writer-busy failures as clean retry skips."
        next_step = "Phase 3BB-R47 - Cloud Scheduler Writer-Gate Runtime Verification"
        command = (
            "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
            "--output-dir reports/phase3bb_r40 --reports-dir reports"
        )
    elif not apply:
        status = "READY_TO_APPLY_SCHEDULER_WRITER_GATE_REPAIR"
        reason = "The patched runner is ready; rerun R46 with --apply --backup-first --reset-failed."
        next_step = "Phase 3BB-R46 - Apply Scheduler Writer-Gate Repair"
        command = (
            "kalshi-bot phase3bb-r46-cloud-scheduler-weather-writer-gate-repair "
            "--output-dir reports/phase3bb_r46 --reports-dir reports "
            "--apply --backup-first --reset-failed"
        )
    elif install_result.get("ok") and verify_after:
        status = "SCHEDULER_WRITER_GATE_REPAIR_INSTALLED"
        reason = "The cloud runner was backed up and patched; writer-busy weather jobs now clean-skip for retry."
        next_step = "Phase 3BB-R47 - Cloud Scheduler Writer-Gate Runtime Verification"
        command = (
            "kalshi-bot phase3bb-r40-cloud-scheduler-runtime-monitor "
            "--output-dir reports/phase3bb_r40 --reports-dir reports\n"
            "kalshi-bot phase3bb-r45-weather-freshness-to-ranking-impact "
            "--output-dir reports/phase3bb_r45 --reports-dir reports"
        )
    else:
        status = "BLOCKED_SCHEDULER_WRITER_GATE_REPAIR_INSTALL"
        reason = "The install attempt did not verify the mid-run writer gate in the cloud runner."
        next_step = "Phase 3BB-R46 - Inspect Scheduler Writer-Gate Repair Install Failure"
        command = (
            "kalshi-bot phase3bb-r46-cloud-scheduler-weather-writer-gate-repair "
            "--output-dir reports/phase3bb_r46 --reports-dir reports"
        )
    return {
        "status": status,
        "repair_ready_or_installed": status
        in {
            "SCHEDULER_WRITER_GATE_ALREADY_REPAIRED",
            "READY_TO_APPLY_SCHEDULER_WRITER_GATE_REPAIR",
            "SCHEDULER_WRITER_GATE_REPAIR_INSTALLED",
        },
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "apply_requested": apply,
        "backup_first": backup_first,
        "reset_failed": reset_failed,
        "runner_patch_required": parsed.get("runner_patch_required"),
        "runner_repaired_before": parsed.get("runner_has_midrun_writer_gate"),
        "runner_repaired_after": verify_after,
        "install_attempted": install_result.get("attempted"),
        "service_result_before": parsed.get("scheduler_service_result"),
        "service_result_after": service_result_after,
        "will_create_paper_trades": False,
        "will_submit_live_or_demo_orders": False,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _can_apply(checks: list[dict[str, Any]], parsed: dict[str, Any]) -> bool:
    return all(row["passed"] for row in checks) and not bool(parsed.get("runner_has_midrun_writer_gate"))


def _build_install_probe(
    target: CloudBootstrapTarget,
    *,
    patched_runner: str,
    backup_first: bool,
    reset_failed: bool,
    timeout_seconds: int,
) -> RemoteProbe:
    encoded = base64.b64encode(patched_runner.encode("utf-8")).decode("ascii")
    script = f"""
import base64
import os
import pathlib
import shutil
import subprocess
import time

runner = pathlib.Path({str(_runner_path(target))!r})
tmp = runner.with_name(runner.name + ".phase3bb_r46.tmp")
backup = runner.with_name(runner.name + ".phase3bb_r46_" + time.strftime("%Y%m%d%H%M%S") + ".bak")
tmp.write_text(base64.b64decode({encoded!r}).decode("utf-8"), encoding="utf-8")
tmp.chmod(0o755)
if {bool(backup_first)!r} and runner.exists():
    shutil.copy2(runner, backup)
os.replace(tmp, runner)
runner.chmod(0o755)
reset_exit = None
reset_stdout = ""
reset_stderr = ""
if {bool(reset_failed)!r}:
    completed = subprocess.run(
        ["systemctl", "reset-failed", {SCHEDULER_SERVICE_NAME!r}],
        capture_output=True,
        text=True,
        check=False,
    )
    reset_exit = completed.returncode
    reset_stdout = completed.stdout.strip()
    reset_stderr = completed.stderr.strip()
print("INSTALLED_R46_SCHEDULER_WRITER_GATE_REPAIR")
print(f"runner={{runner}}")
print(f"backup={{backup if backup.exists() else ''}}")
print(f"reset_failed_exit={{reset_exit}}")
if reset_stdout:
    print(f"reset_failed_stdout={{reset_stdout}}")
if reset_stderr:
    print(f"reset_failed_stderr={{reset_stderr}}")
"""
    command = "python3 - <<'PY'\n" + script.strip() + "\nPY"
    return RemoteProbe("install_runner_writer_gate_repair", command, timeout_seconds)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R46 Cloud Scheduler Weather Writer-Gate Repair")
    decision = payload["repair_decision"]
    parsed = payload["parsed_repair_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Apply requested: `{decision['apply_requested']}`",
            f"- Backup first: `{decision['backup_first']}`",
            f"- Reset failed marker: `{decision['reset_failed']}`",
            f"- Runner repaired before: `{decision['runner_repaired_before']}`",
            f"- Runner repaired after: `{decision['runner_repaired_after']}`",
            f"- Runner patch required: `{decision['runner_patch_required']}`",
            f"- Scheduler service result before: `{decision['service_result_before']}`",
            f"- Scheduler service result after: `{decision['service_result_after']}`",
            f"- Scheduler timer: `{parsed.get('scheduler_timer_active_state')}`",
            f"- Writer status: `{parsed.get('writer_status')}`",
            f"- R45 blocker: `{parsed.get('r45_first_weather_blocker')}`",
            "",
            "## Repair",
            "",
            "Writer-gated scheduler jobs now capture command output and convert mid-run SQLite writer contention into a clean retry skip:",
            "",
            "```bash",
            "Writer became active during ${job_id}; clean skip for retry",
            "```",
            "",
            "## Safety",
            "",
            "- Paper trade creation: `False`",
            "- Live/demo order submission/cancel/replace: `False`",
            "- Scheduler service/timer start/stop/restart: `0`",
            "- R5 start/stop: `0`",
            "- DB writes: `0`",
            "",
            "## Next",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R46 Repair Detail")
    lines.extend(["", "## Checks", "", "| Check | Passed | Detail |", "|---|---:|---|"])
    for row in payload["repair_checks"]:
        lines.append(f"| `{row['check']}` | `{row['passed']}` | {row['detail']} |")
    install = payload["install_result"]
    lines.extend(
        [
            "",
            "## Install Result",
            "",
            f"- Attempted: `{install.get('attempted')}`",
            f"- OK: `{install.get('ok')}`",
            f"- Exit code: `{install.get('exit_code')}`",
            f"- Stdout: `{install.get('stdout')}`",
            f"- Stderr: `{install.get('stderr')}`",
            "",
            "## Patched Run Job Block",
            "",
            "```bash",
            RUN_JOB_BLOCK,
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["repair_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R46 Next Actions")
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
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
            "- Do not manually stop R5 just to clear scheduler writer contention.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R46 next safe operator command.",
            payload["repair_decision"]["operator_next_command"],
            "",
        ]
    )


def _runner_path(target: CloudBootstrapTarget) -> str:
    return f"{target.app_path.rstrip('/')}/scripts/{RUNNER_SCRIPT_NAME}"


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _write_rows_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}
