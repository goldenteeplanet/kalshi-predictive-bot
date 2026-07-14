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
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r36_cloud_scheduler_install_handoff import (
    SCHEDULER_SERVICE_NAME,
    SCHEDULER_TIMER_NAME,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R38_TIMER_START_VERSION = "phase3bb_r38_cloud_scheduler_timer_start_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r38")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
APPROVAL_ENV_VAR = "PHASE3BB_R38_TIMER_START"
APPROVAL_TOKEN = "I_APPROVE_R38_TIMER_START"
ROOT_TIMER_START_REMOTE_PATH = "/tmp/phase3bb_r38_root_console_scheduler_timer_start.sh"
READY_R37_STATUS = "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START"


@dataclass(frozen=True)
class Phase3BBR38CloudSchedulerTimerStartHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_handoff_script_path: Path
    root_console_script_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r38_cloud_scheduler_timer_start_handoff_report(
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
) -> Phase3BBR38CloudSchedulerTimerStartHandoffArtifacts:
    payload = build_phase3bb_r38_cloud_scheduler_timer_start_handoff(
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
    markdown_path = output_dir / "cloud_scheduler_timer_start_handoff.md"
    json_path = output_dir / "cloud_scheduler_timer_start_handoff.json"
    probe_csv_path = output_dir / "timer_start_remote_probe_results.csv"
    checks_csv_path = output_dir / "timer_start_checks.csv"
    operator_handoff_script_path = output_dir / "operator_scheduler_timer_start_handoff.sh"
    root_console_script_path = output_dir / "root_console_scheduler_timer_start.sh"
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
    _write_checks_csv(checks_csv_path, payload["timer_start_checks"])
    operator_handoff_script_path.write_text(_render_operator_handoff_script(payload), encoding="utf-8")
    _mark_executable(operator_handoff_script_path)
    root_console_script_path.write_text(_render_root_console_script(payload), encoding="utf-8")
    _mark_executable(root_console_script_path)
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
            operator_handoff_script_path,
            root_console_script_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR38CloudSchedulerTimerStartHandoffArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        operator_handoff_script_path=operator_handoff_script_path,
        root_console_script_path=root_console_script_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r38_cloud_scheduler_timer_start_handoff(
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
        "command": "kalshi-bot phase3bb-r38-cloud-scheduler-timer-start-handoff",
        "argv": command_args or [],
    }
    r11_path = reports_dir / "phase3bb_r11" / "codex_cloud_context.json"
    r37_path = reports_dir / "phase3bb_r37" / "cloud_scheduler_install_verification.json"
    r11 = _read_json(r11_path)
    r37 = _read_json(r37_path)
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
    commands = _handoff_commands(target=target)
    checks = _timer_start_checks(r11=r11, r37=r37, parsed=parsed, commands=commands)
    decision = _timer_start_decision(checks, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "timer_start_handoff_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 0,
        "systemctl_mutating_commands_executed": 0,
        "timer_start_executed_by_codex": False,
        "scheduler_timer_started_by_codex": False,
        "scheduler_service_started_by_codex": False,
        "runs_refresh_jobs": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "remote_db_writes_performed": 0,
        "local_db_writes_performed": 0,
        "creates_paper_trades": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "secrets_printed": False,
    }
    return {
        **metadata,
        "phase": "3BB-R38-CLOUD-SCHEDULER-TIMER-START-HANDOFF",
        "phase_version": PHASE3BB_R38_TIMER_START_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_TIMER_START_HANDOFF",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r37_artifact_path": str(r37_path),
        "r11_context_available": bool(r11),
        "r37_context_available": bool(r37),
        "cloud_target": _target_payload(target),
        "r37_verification_decision": r37.get("verification_decision") or {},
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "handoff_commands": commands,
        "timer_start_checks": checks,
        "timer_start_decision": decision,
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
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    source_env = f"set -a && . {env} && set +a"
    service = shlex.quote(SCHEDULER_SERVICE_NAME)
    timer = shlex.quote(SCHEDULER_TIMER_NAME)
    return [
        RemoteProbe(
            "scheduler_systemd_state",
            (
                f"systemctl show {service} {timer} --no-pager "
                "-p Id -p LoadState -p UnitFileState -p ActiveState -p SubState "
                "-p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe("scheduler_timer_enabled", f"systemctl is-enabled {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_timer_active", f"systemctl is-active {timer} || true", timeout_seconds),
        RemoteProbe("scheduler_service_active", f"systemctl is-active {service} || true", timeout_seconds),
        RemoteProbe(
            "r8_command_registry",
            f"cd {app} && .venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate --help "
            ">/tmp/phase3bb_r38_timer_start_r8_help.txt && echo R8_REGISTERED",
            timeout_seconds,
        ),
        RemoteProbe(
            "sudo_noninteractive_true",
            "sudo -n true >/dev/null 2>&1 && echo SUDO_N_OK || echo SUDO_N_BLOCKED",
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r38_timer_start_r5.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "db_writer_monitor",
            f"cd {app} && {source_env} && .venv/bin/kalshi-bot db-writer-monitor --json",
            timeout_seconds,
        ),
    ]


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    units = _parse_multi_unit_systemd(_stdout(by_name.get("scheduler_systemd_state")))
    r5_status = _json_from_probe(by_name.get("r5_status"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    guard = r5_status.get("guard") if isinstance(r5_status, dict) else {}
    latest_summary = r5_status.get("latest_summary") if isinstance(r5_status, dict) else {}
    pids = [_to_int(pid) for pid in (process or {}).get("phase3bc_r5_pids") or []]
    pids = [pid for pid in pids if pid is not None]
    r5_pid = _to_int(r5_status.get("pid") if isinstance(r5_status, dict) else None)
    if r5_pid is None and pids:
        r5_pid = pids[0]
    service_unit = units.get(SCHEDULER_SERVICE_NAME, {})
    timer_unit = units.get(SCHEDULER_TIMER_NAME, {})
    return {
        "systemd_units": units,
        "scheduler_service_loaded": service_unit.get("LoadState") == "loaded",
        "scheduler_timer_loaded": timer_unit.get("LoadState") == "loaded",
        "scheduler_timer_enabled": _first_line(_stdout(by_name.get("scheduler_timer_enabled")))
        == "enabled"
        or timer_unit.get("UnitFileState") == "enabled",
        "scheduler_timer_active": _first_line(_stdout(by_name.get("scheduler_timer_active")))
        == "active"
        or timer_unit.get("ActiveState") == "active",
        "scheduler_service_active": _first_line(_stdout(by_name.get("scheduler_service_active")))
        == "active"
        or service_unit.get("ActiveState") == "active",
        "r8_registered": bool(
            by_name.get("r8_command_registry") and by_name["r8_command_registry"].ok
        ),
        "sudo_noninteractive_true": "SUDO_N_OK"
        in _stdout(by_name.get("sudo_noninteractive_true")),
        "r5_status": r5_status,
        "r5_running": bool((process or {}).get("phase3bc_r5_process_running")),
        "r5_pid": r5_pid,
        "r5_pids": pids,
        "duplicate_r5": len(pids) > 1,
        "guard_status": (guard or {}).get("status"),
        "guard_should_stop": bool((guard or {}).get("should_stop")),
        "watch_state": r5_status.get("latest_watch_state") if isinstance(r5_status, dict) else None,
        "paper_ready_candidates": (latest_summary or {}).get("paper_ready_candidates"),
        "positive_ev_rows": (latest_summary or {}).get("positive_ev_rows"),
        "db_writer_monitor": writer,
        "writer_status": writer.get("status") if isinstance(writer, dict) else "UNKNOWN",
        "writer_safe_to_start_write": bool(writer.get("safe_to_start_write"))
        if isinstance(writer, dict)
        else False,
        "writer_pid": _to_int(
            writer.get("current_writer_pid") if isinstance(writer, dict) else None
        ),
    }


def _handoff_commands(*, target: CloudBootstrapTarget) -> dict[str, str]:
    ssh_target = str(target.ssh_target)
    identity_file = str(target.identity_file)
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    scp_prefix = f"scp -i {_shell_quote(identity_file)}"
    root_local = "reports/phase3bb_r38/root_console_scheduler_timer_start.sh"
    return {
        "copy_root_console_timer_start": (
            f"{scp_prefix} {root_local} "
            f"{_shell_quote(f'{ssh_target}:{ROOT_TIMER_START_REMOTE_PATH}')}"
        ),
        "start_timer_with_sudo_n": (
            f"{ssh_prefix} 'sudo -n systemctl start {SCHEDULER_TIMER_NAME} && "
            f"systemctl is-active {SCHEDULER_TIMER_NAME} && "
            f"systemctl list-timers --all {SCHEDULER_TIMER_NAME} --no-pager'"
        ),
        "root_console_timer_start": f"bash {ROOT_TIMER_START_REMOTE_PATH}",
        "verify_after_timer_start": (
            "kalshi-bot phase3bb-r37-cloud-scheduler-install-verification "
            "--output-dir reports/phase3bb_r37 --reports-dir reports"
        ),
    }


def _timer_start_checks(
    *,
    r11: dict[str, Any],
    r37: dict[str, Any],
    parsed: dict[str, Any],
    commands: dict[str, str],
) -> list[dict[str, Any]]:
    r37_decision = r37.get("verification_decision") or {}
    combined = "\n".join(commands.values()).lower()
    return [
        _check("r11_cloud_context_present", bool(r11), "R11 cloud context exists."),
        _check("r37_verification_present", bool(r37), "R37 verification artifact exists."),
        _check(
            "r37_verified_enable_no_start",
            r37_decision.get("status") == READY_R37_STATUS
            and bool(r37_decision.get("verification_passed")),
            f"R37 status is {r37_decision.get('status')}.",
        ),
        _check(
            "scheduler_service_installed",
            bool(parsed.get("scheduler_service_loaded")),
            "Scheduler service LoadState is loaded.",
        ),
        _check(
            "scheduler_timer_installed",
            bool(parsed.get("scheduler_timer_loaded")),
            "Scheduler timer LoadState is loaded.",
        ),
        _check(
            "scheduler_timer_enabled",
            bool(parsed.get("scheduler_timer_enabled")),
            "Scheduler timer is enabled.",
        ),
        _check(
            "r8_registered_on_cloud",
            bool(parsed.get("r8_registered")),
            "Cloud command registry contains phase3bb-r8-unified-paper-gate.",
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
            "start_handoff_does_not_run_trades",
            not any(
                fragment in combined
                for fragment in (
                    "accelerate-learning",
                    "autopilot",
                    "create-paper-trade",
                    "live-order",
                    "place-order",
                    "submit-order",
                )
            ),
            "Timer start handoff contains no paper/live/demo trade commands.",
        ),
        _check(
            "start_handoff_starts_timer_only",
            f"systemctl start {SCHEDULER_TIMER_NAME}" in combined
            and f"systemctl start {SCHEDULER_SERVICE_NAME}" not in combined,
            "Handoff starts the timer, not the oneshot service directly.",
        ),
    ]


def _timer_start_decision(
    checks: list[dict[str, Any]],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    if parsed.get("scheduler_timer_active") and not failed:
        status = "SCHEDULER_TIMER_ALREADY_ACTIVE"
        reason = "The scheduler timer is already active; no start handoff is needed."
        next_command = (
            "systemctl list-timers --all kalshi-multicategory-refresh-scheduler.timer --no-pager"
        )
        next_step = "Phase 3BB-R40 - Cloud Scheduler Runtime Monitor"
    elif failed:
        status = "BLOCKED_TIMER_START_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
        next_command = (
            "kalshi-bot phase3bb-r37-cloud-scheduler-install-verification "
            "--output-dir reports/phase3bb_r37 --reports-dir reports"
        )
        next_step = "Phase 3BB-R37 - Resolve Failed Timer Start Preconditions"
    else:
        status = "READY_FOR_OPERATOR_APPROVED_TIMER_START"
        reason = (
            "R37 verified install+enable-no-start, R8 is registered on cloud, R5 is "
            "healthy, and the generated start handoff starts only the systemd timer."
        )
        next_command = (
            f"{APPROVAL_ENV_VAR}={APPROVAL_TOKEN} "
            "bash reports/phase3bb_r38/operator_scheduler_timer_start_handoff.sh"
        )
        next_step = "Phase 3BB-R40 - Cloud Scheduler Runtime Monitor After Timer Start"
    return {
        "status": status,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "scheduler_service_installed": bool(parsed.get("scheduler_service_loaded")),
        "scheduler_timer_installed": bool(parsed.get("scheduler_timer_loaded")),
        "scheduler_timer_enabled": bool(parsed.get("scheduler_timer_enabled")),
        "scheduler_timer_active": bool(parsed.get("scheduler_timer_active")),
        "scheduler_service_active": bool(parsed.get("scheduler_service_active")),
        "r8_registered": bool(parsed.get("r8_registered")),
        "sudo_noninteractive_true": bool(parsed.get("sudo_noninteractive_true")),
        "r5_pid": parsed.get("r5_pid"),
        "duplicate_r5": bool(parsed.get("duplicate_r5")),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "watch_state": parsed.get("watch_state"),
        "paper_ready_candidates": parsed.get("paper_ready_candidates"),
        "positive_ev_rows": parsed.get("positive_ev_rows"),
        "writer_pid": parsed.get("writer_pid"),
        "codex_started_timer": False,
        "codex_started_service": False,
        "codex_created_trades": False,
        "operator_next_command": next_command,
        "next_codex_step": next_step,
    }


def _render_operator_handoff_script(payload: dict[str, Any]) -> str:
    commands = payload["handoff_commands"]
    decision = payload["timer_start_decision"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TOKEN=${{{APPROVAL_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(APPROVAL_TOKEN)}",
        "",
        "echo '[phase3bb-r38] scheduler timer start handoff'",
        "echo '[phase3bb-r38] default mode is dry-run; timer is not started'",
        "",
        f"COPY_ROOT_SCRIPT={_shell_quote(commands['copy_root_console_timer_start'])}",
        f"START_TIMER={_shell_quote(commands['start_timer_with_sudo_n'])}",
        "",
        "if [[ \"$TOKEN\" != \"$REQUIRED\" ]]; then",
        "  echo '[phase3bb-r38] dry-run command list:'",
        "  printf '  %s\\n' \"$COPY_ROOT_SCRIPT\"",
        "  printf '  %s\\n' \"$START_TIMER\"",
        "  echo '[phase3bb-r38] no timer start executed'",
        f"  echo \"[phase3bb-r38] to execute: {APPROVAL_ENV_VAR}=$REQUIRED bash $0\"",
        "  exit 0",
        "fi",
        "",
        "echo '[phase3bb-r38] approval token accepted'",
        "bash -lc \"$COPY_ROOT_SCRIPT\"",
        "if bash -lc \"$START_TIMER\"; then",
        "  echo '[phase3bb-r38] scheduler timer started via sudo -n'",
        "else",
        "  echo '[phase3bb-r38] sudo -n timer start failed; use root console:'",
        f"  echo '  {commands['root_console_timer_start']}'",
        "  exit 3",
        "fi",
        f"echo '[phase3bb-r38] next: {decision['next_codex_step']}'",
        "",
    ]
    return "\n".join(lines)


def _render_root_console_script(payload: dict[str, Any]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TIMER={_shell_quote(SCHEDULER_TIMER_NAME)}",
        f"SERVICE={_shell_quote(SCHEDULER_SERVICE_NAME)}",
        "",
        "if [[ \"$(id -u)\" -ne 0 ]]; then",
        "  echo '[phase3bb-r38] run this script as root from the cloud console'",
        "  exit 2",
        "fi",
        "",
        "echo '[phase3bb-r38] starting scheduler timer only'",
        "systemctl start \"${TIMER}\"",
        "systemctl is-active \"${TIMER}\"",
        "systemctl list-timers --all \"${TIMER}\" --no-pager || true",
        "systemctl status \"${TIMER}\" \"${SERVICE}\" --no-pager || true",
        "echo '[phase3bb-r38] timer start complete; rerun R37/R40 monitor next'",
        "",
    ]
    return "\n".join(lines)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Cloud Scheduler Timer Start Handoff")
    decision = payload["timer_start_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Handoff ready: `{decision['handoff_ready']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Scheduler",
            "",
            f"- Service installed: `{decision['scheduler_service_installed']}`",
            f"- Service active: `{decision['scheduler_service_active']}`",
            f"- Timer installed: `{decision['scheduler_timer_installed']}`",
            f"- Timer enabled: `{decision['scheduler_timer_enabled']}`",
            f"- Timer active: `{decision['scheduler_timer_active']}`",
            f"- R8 registered: `{decision['r8_registered']}`",
            f"- Noninteractive sudo true: `{decision['sudo_noninteractive_true']}`",
            "",
            "## R5",
            "",
            f"- PID: `{decision['r5_pid']}`",
            f"- Duplicate R5: `{decision['duplicate_r5']}`",
            f"- Guard status: `{decision['guard_status']}`",
            f"- Guard should stop: `{decision['guard_should_stop']}`",
            f"- Watch state: `{decision['watch_state']}`",
            f"- Paper-ready candidates: `{decision['paper_ready_candidates']}`",
            "",
            "## Safety",
            "",
            "- Codex did not start the scheduler timer.",
            "- Codex did not start the scheduler service directly.",
            "- Codex did not stop or duplicate R5.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Operator Command",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Timer Start Handoff Detail")
    decision = payload["timer_start_decision"]
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
    for row in payload["timer_start_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Commands", ""])
    for name, command in payload["handoff_commands"].items():
        lines.extend([f"### {name}", "", "```bash", command, "```", ""])
    lines.extend(["", "## Parsed Remote State", "", "```json"])
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.extend(["```"])
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["timer_start_decision"]["operator_next_command"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", "", command, ""])


def _render_next_actions(payload: dict[str, Any]) -> str:
    decision = payload["timer_start_decision"]
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Timer Start Next Actions")
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "If sudo still blocks the local handoff, open the cloud root console and run:",
            "",
            "```bash",
            f"bash {ROOT_TIMER_START_REMOTE_PATH}",
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not start the oneshot service directly.",
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _parse_multi_unit_systemd(text: str) -> dict[str, dict[str, str]]:
    units: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    current_id = ""
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "Id":
            if current_id:
                units[current_id] = current
            current_id = value
            current = {"Id": value}
        elif current_id:
            current[key] = value
    if current_id:
        units[current_id] = current
    return units


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


def _to_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


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
