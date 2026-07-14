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
    CloudBootstrapTarget,
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    DEFAULT_SERVICE_NAME,
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R19_VERSION = "phase3bb_r19_cloud_systemd_cutover_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r19")
APPROVAL_ENV_VAR = "PHASE3BB_R19_EXECUTE"
APPROVAL_TOKEN = "I_APPROVE_R19_CUTOVER"
DEFAULT_GRACE_SECONDS = 45


@dataclass(frozen=True)
class Phase3BBR19CloudSystemdCutoverArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    cutover_checks_path: Path
    remote_results_path: Path
    operator_cutover_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r19_cloud_systemd_cutover_report(
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
    control_ssh_target: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    expected_r5_pid: int | None = None,
    execute: bool = False,
    approval_token: str | None = None,
    terminate_grace_seconds: int = DEFAULT_GRACE_SECONDS,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR19CloudSystemdCutoverArtifacts:
    payload = build_phase3bb_r19_cloud_systemd_cutover(
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
        control_ssh_target=control_ssh_target,
        service_name=service_name,
        expected_r5_pid=expected_r5_pid,
        execute=execute,
        approval_token=approval_token,
        terminate_grace_seconds=terminate_grace_seconds,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_systemd_cutover.md"
    json_path = output_dir / "cloud_systemd_cutover.json"
    cutover_checks_path = output_dir / "cutover_checks.csv"
    remote_results_path = output_dir / "remote_cutover_results.csv"
    operator_cutover_command_path = output_dir / "operator_cutover_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_checks_csv(cutover_checks_path, payload["cutover_checks"])
    _write_remote_results_csv(remote_results_path, payload["remote_cutover_results"])
    operator_cutover_command_path.write_text(
        _render_operator_cutover_command(payload),
        encoding="utf-8",
    )
    _mark_executable(operator_cutover_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            cutover_checks_path,
            remote_results_path,
            operator_cutover_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR19CloudSystemdCutoverArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        cutover_checks_path=cutover_checks_path,
        remote_results_path=remote_results_path,
        operator_cutover_command_path=operator_cutover_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r19_cloud_systemd_cutover(
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
    control_ssh_target: str | None = None,
    service_name: str = DEFAULT_SERVICE_NAME,
    expected_r5_pid: int | None = None,
    execute: bool = False,
    approval_token: str | None = None,
    terminate_grace_seconds: int = DEFAULT_GRACE_SECONDS,
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
        "command": "kalshi-bot phase3bb-r19-cloud-systemd-cutover",
        "argv": command_args or [],
    }
    runner = probe_runner or _run_ssh_probe
    pre_r18 = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
        session,
        output_dir=output_dir / "preflight_r18",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r18-cloud-scheduler-runtime-cutover"],
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        service_name=service_name,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=runner,
    )
    inspect_target = _target_from_payload(pre_r18["cloud_target"])
    control_target = _control_target(inspect_target, control_ssh_target=control_ssh_target)
    expected_pid = expected_r5_pid or _to_int(
        pre_r18.get("runtime_cutover_decision", {}).get("expected_existing_r5_pid")
    ) or _to_int(pre_r18.get("runtime_cutover_decision", {}).get("current_r5_pid"))
    approval_valid = approval_token == APPROVAL_TOKEN
    checks = _cutover_checks(
        pre_r18=pre_r18,
        expected_pid=expected_pid,
        execute=execute,
        approval_valid=approval_valid,
    )
    blocking = [row for row in checks if not row["passed"] and row["severity"] == "BLOCKING"]
    remote_results: list[RemoteProbeResult] = []
    post_r18: dict[str, Any] | None = None
    mutation_started = time.monotonic()
    mutation_attempted = execute and approval_valid and not blocking
    if mutation_attempted:
        probes = _build_cutover_mutation_probes(
            pre_r18=pre_r18,
            expected_pid=expected_pid,
            service_name=service_name,
            terminate_grace_seconds=terminate_grace_seconds,
            timeout_seconds=per_probe_timeout_seconds,
        )
        for probe in probes:
            result = runner(probe, control_target)
            remote_results.append(result)
            if not result.ok:
                break
        if all(result.ok for result in remote_results):
            post_r18 = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
                session,
                output_dir=output_dir / "post_cutover_r18",
                reports_dir=reports_dir,
                settings=resolved,
                command_args=["phase3bb-r18-cloud-scheduler-runtime-cutover"],
                ssh_target=ssh_target,
                identity_file=identity_file,
                app_path=app_path,
                env_path=env_path,
                db_path=db_path,
                service_name=service_name,
                per_probe_timeout_seconds=per_probe_timeout_seconds,
                probe_runner=runner,
            )
    mutation_duration = round(time.monotonic() - mutation_started, 3)
    decision = _cutover_decision(
        checks,
        pre_r18=pre_r18,
        post_r18=post_r18,
        remote_results=remote_results,
        execute=execute,
        approval_valid=approval_valid,
        mutation_attempted=mutation_attempted,
    )
    remote_results_payload = [_result_payload(result) for result in remote_results]
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": not mutation_attempted,
        "operator_approved_cutover": mutation_attempted,
        "approval_token_value_recorded": False,
        "approval_token_present": bool(approval_token),
        "approval_token_valid": approval_valid,
        "remote_commands_executed": len(remote_results_payload),
        "remote_db_writes_performed": 0,
        "remote_report_writes_only": not mutation_attempted,
        "systemctl_mutating_commands_executed": _count_mutation(remote_results, "systemd_start"),
        "systemctl_read_only_commands_executed": 0,
        "service_files_written_to_system": False,
        "starts_r5_watcher": _count_mutation(remote_results, "systemd_start") > 0,
        "starts_service": _count_mutation(remote_results, "systemd_start") > 0,
        "starts_duplicate_watchers": False,
        "stops_processes": _count_mutation(remote_results, "manual_r5_graceful_sigterm") > 0,
        "stops_expected_r5_pid_only": _count_mutation(
            remote_results,
            "manual_r5_graceful_sigterm",
        )
        > 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R19-CLOUD-SYSTEMD-CUTOVER",
        "phase_version": PHASE3BB_R19_VERSION,
        "mode": "PAPER_SAFE_OPERATOR_APPROVED_CLOUD_SYSTEMD_CUTOVER",
        "reports_dir": str(reports_dir),
        "service_name": service_name,
        "expected_r5_pid": expected_pid,
        "terminate_grace_seconds": terminate_grace_seconds,
        "execute_requested": execute,
        "approval_env_var": APPROVAL_ENV_VAR,
        "approval_token_required": APPROVAL_TOKEN,
        "approval_token_present": bool(approval_token),
        "approval_token_valid": approval_valid,
        "inspect_target": _target_payload(inspect_target),
        "control_target": _target_payload(control_target),
        "preflight_r18": pre_r18,
        "post_cutover_r18": post_r18,
        "cutover_checks": checks,
        "remote_cutover_results": remote_results_payload,
        "remote_cutover_duration_seconds": mutation_duration,
        "cutover_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _cutover_checks(
    *,
    pre_r18: dict[str, Any],
    expected_pid: int | None,
    execute: bool,
    approval_valid: bool,
) -> list[dict[str, Any]]:
    decision = pre_r18.get("runtime_cutover_decision") or {}
    parsed = pre_r18.get("parsed_remote_state") or {}
    status = decision.get("status")
    current_pid = _to_int(decision.get("current_r5_pid"))
    service_started = bool(decision.get("service_started"))
    service_enabled = bool(decision.get("service_enabled"))
    duplicate_r5 = bool(decision.get("duplicate_r5"))
    guard_status = decision.get("guard_status")
    guard_should_stop = bool(decision.get("guard_should_stop"))
    writer_pid = _to_int(decision.get("writer_pid"))
    writer_ok = writer_pid is None or writer_pid == current_pid
    state_allowed = status in {
        "WAIT_FOR_MANUAL_R5_TO_EXIT",
        "READY_FOR_SYSTEMD_START",
        "SYSTEMD_OWNS_R5",
    }
    manual_state = status == "WAIT_FOR_MANUAL_R5_TO_EXIT"
    already_exited_state = status == "READY_FOR_SYSTEMD_START"
    complete_state = status == "SYSTEMD_OWNS_R5"
    return [
        _check(
            "r18_monitor_passed",
            bool(decision.get("monitor_passed")) and not decision.get("failed_check_count"),
            f"R18 status={status}; failed_check_count={decision.get('failed_check_count')}.",
        ),
        _check(
            "cutover_state_allowed",
            state_allowed,
            f"R18 status={status}.",
        ),
        _check(
            "service_enabled",
            service_enabled,
            f"service_enabled={service_enabled}.",
        ),
        _check(
            "service_inactive_before_cutover",
            complete_state or not service_started,
            f"service_started={service_started}.",
        ),
        _check(
            "no_duplicate_r5",
            not duplicate_r5,
            f"R5 PIDs={parsed.get('r5_pids')}.",
        ),
        _check(
            "expected_manual_r5_pid_or_already_exited",
            complete_state
            or already_exited_state
            or (expected_pid is not None and current_pid == expected_pid),
            f"expected_pid={expected_pid}; current_pid={current_pid}.",
        ),
        _check(
            "writer_clear_or_expected_r5",
            writer_ok,
            f"writer_pid={writer_pid}; current_pid={current_pid}.",
        ),
        _check(
            "guard_allows_operator_cutover",
            complete_state
            or already_exited_state
            or (guard_status == "RUNNING" and guard_should_stop is False),
            f"guard_status={guard_status}; guard_should_stop={guard_should_stop}.",
        ),
        _check(
            "manual_r5_or_ready_to_start",
            manual_state or already_exited_state or complete_state,
            f"manual_state={manual_state}; already_exited_state={already_exited_state}.",
        ),
        _check(
            "approval_token_for_execution",
            (not execute) or approval_valid,
            "Valid approval token supplied."
            if approval_valid
            else "Execution requires approval token.",
            severity="BLOCKING" if execute else "INFO",
        ),
    ]


def _build_cutover_mutation_probes(
    *,
    pre_r18: dict[str, Any],
    expected_pid: int | None,
    service_name: str,
    terminate_grace_seconds: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    status = (pre_r18.get("runtime_cutover_decision") or {}).get("status")
    service = shlex.quote(service_name)
    probes = [
        RemoteProbe(
            "control_identity",
            "set -euo pipefail; whoami; hostname",
            timeout_seconds,
        )
    ]
    if status == "WAIT_FOR_MANUAL_R5_TO_EXIT":
        if expected_pid is None:
            probes.append(RemoteProbe("missing_expected_pid", "exit 64", timeout_seconds))
        else:
            probes.extend(
                [
                    RemoteProbe(
                        "manual_r5_graceful_sigterm",
                        _sigterm_command(expected_pid, terminate_grace_seconds),
                        max(timeout_seconds, terminate_grace_seconds + 10),
                    ),
                    RemoteProbe(
                        "verify_manual_r5_exited",
                        _verify_pid_exited_command(expected_pid),
                        timeout_seconds,
                    ),
                ]
            )
    probes.extend(
        [
            RemoteProbe(
                "systemd_start",
                f"set -euo pipefail; systemctl start {service}",
                timeout_seconds,
            ),
            RemoteProbe(
                "systemd_show_after_start",
                (
                    f"systemctl show {service} --no-pager -p LoadState -p UnitFileState "
                    "-p ActiveState -p SubState -p FragmentPath -p ExecMainPID"
                ),
                timeout_seconds,
            ),
        ]
    )
    return probes


def _sigterm_command(pid: int, grace_seconds: int) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            f"pid={pid}",
            f"grace={grace_seconds}",
            'if ! kill -0 "$pid" 2>/dev/null; then echo "PID_ALREADY_EXITED"; exit 0; fi',
            'cmd="$(ps -p "$pid" -o args= || true)"',
            'case "$cmd" in',
            '  *phase3bc-r5-crypto-freshness-watch*) ;;',
            '  *) echo "UNEXPECTED_PROCESS:$cmd"; exit 65 ;;',
            "esac",
            'kill -TERM "$pid"',
            'for _ in $(seq 1 "$grace"); do',
            '  if ! kill -0 "$pid" 2>/dev/null; then echo "SIGTERM_EXITED"; exit 0; fi',
            "  sleep 1",
            "done",
            'echo "PID_STILL_RUNNING_AFTER_SIGTERM"',
            "exit 66",
        ]
    )


def _verify_pid_exited_command(pid: int) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            f"pid={pid}",
            'if kill -0 "$pid" 2>/dev/null; then',
            '  echo "PID_STILL_RUNNING"',
            "  exit 67",
            "fi",
            'echo "PID_EXITED"',
        ]
    )


def _cutover_decision(
    checks: list[dict[str, Any]],
    *,
    pre_r18: dict[str, Any],
    post_r18: dict[str, Any] | None,
    remote_results: list[RemoteProbeResult],
    execute: bool,
    approval_valid: bool,
    mutation_attempted: bool,
) -> dict[str, Any]:
    blocking = [row for row in checks if not row["passed"] and row["severity"] == "BLOCKING"]
    failed_remote = [result for result in remote_results if not result.ok]
    pre_status = (pre_r18.get("runtime_cutover_decision") or {}).get("status")
    if pre_status == "SYSTEMD_OWNS_R5" and not blocking:
        status = "CUTOVER_ALREADY_COMPLETE_SYSTEMD_OWNS_R5"
        action = "MONITOR_SYSTEMD_R5"
        reason = "Systemd is already the single R5 owner; no cutover command is needed."
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R20 - Cloud UI Service Plan"
    elif execute and not approval_valid:
        status = "BLOCKED_APPROVAL_TOKEN_REQUIRED"
        action = "ADD_APPROVAL_TOKEN"
        reason = f"Set {APPROVAL_ENV_VAR}={APPROVAL_TOKEN} and rerun with --execute."
        command = _approved_command()
        next_step = "Phase 3BB-R19 - Rerun With Approval Token"
    elif blocking:
        status = "BLOCKED_CUTOVER_PREFLIGHT"
        action = "FIX_PREFLIGHT"
        reason = f"First failing check: {blocking[0]['check']}."
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R18 - Resolve Runtime Cutover Monitor Blocker"
    elif not execute:
        status = "READY_FOR_OPERATOR_APPROVED_CUTOVER"
        action = "RUN_APPROVED_CUTOVER"
        reason = (
            "Preflight is clean. R19 did not stop R5 or start systemd because "
            "--execute was not requested."
        )
        command = _approved_command()
        next_step = "Phase 3BB-R19 - Run Operator-Approved Cutover Command"
    elif failed_remote:
        status = "CUTOVER_FAILED_REMOTE_COMMAND"
        action = "INSPECT_REMOTE_FAILURE"
        reason = (
            f"Remote command {failed_remote[0].name} failed with exit "
            f"{failed_remote[0].exit_code}."
        )
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R18 - Inspect Post-Failure Runtime State"
    elif post_r18 and (
        (post_r18.get("runtime_cutover_decision") or {}).get("status") == "SYSTEMD_OWNS_R5"
    ):
        status = "CUTOVER_COMPLETE_SYSTEMD_OWNS_R5"
        action = "VERIFY_AND_MONITOR"
        reason = "Manual R5 exited and systemd is now the single R5 owner."
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R20 - Cloud UI Service Plan"
    else:
        post_status = (post_r18 or {}).get("runtime_cutover_decision", {}).get("status")
        status = "CUTOVER_NEEDS_REVIEW"
        action = "RERUN_R18"
        reason = f"Cutover commands completed but post-cutover R18 status is {post_status}."
        command = (
            "kalshi-bot phase3bb-r18-cloud-scheduler-runtime-cutover "
            "--output-dir reports/phase3bb_r18 --reports-dir reports"
        )
        next_step = "Phase 3BB-R18 - Reconcile Runtime State"
    pre_decision = pre_r18.get("runtime_cutover_decision") or {}
    post_decision = (post_r18 or {}).get("runtime_cutover_decision") or {}
    return {
        "status": status,
        "recommended_action": action,
        "primary_reason": reason,
        "preflight_status": pre_decision.get("status"),
        "post_cutover_status": post_decision.get("status"),
        "mutation_attempted": mutation_attempted,
        "remote_failure_count": len(failed_remote),
        "first_failed_remote_command": failed_remote[0].name if failed_remote else None,
        "preflight_failed_check_count": len(blocking),
        "first_failed_preflight_check": blocking[0]["check"] if blocking else None,
        "codex_executed_sigterm": _count_mutation(remote_results, "manual_r5_graceful_sigterm")
        > 0,
        "codex_executed_systemd_start": _count_mutation(remote_results, "systemd_start") > 0,
        "operator_next_command": command,
        "next_codex_step": next_step,
    }


def _approved_command() -> str:
    return (
        f"{APPROVAL_ENV_VAR}={APPROVAL_TOKEN} "
        "kalshi-bot phase3bb-r19-cloud-systemd-cutover "
        "--output-dir reports/phase3bb_r19 --reports-dir reports --execute"
    )


def _check(
    name: str,
    passed: bool,
    detail: str,
    *,
    severity: str = "BLOCKING",
) -> dict[str, Any]:
    return {
        "check": name,
        "passed": bool(passed),
        "detail": detail,
        "severity": severity,
    }


def _target_from_payload(payload: dict[str, Any]) -> CloudBootstrapTarget:
    return CloudBootstrapTarget(
        ssh_target=str(payload.get("ssh_target") or ""),
        identity_file=str(payload.get("identity_file") or ""),
        app_path=str(payload.get("app_path") or ""),
        env_path=str(payload.get("env_path") or ""),
        db_path=str(payload.get("db_path") or ""),
        reports_path=str(payload.get("reports_path") or ""),
    )


def _control_target(
    target: CloudBootstrapTarget,
    *,
    control_ssh_target: str | None,
) -> CloudBootstrapTarget:
    ssh_target = control_ssh_target or _root_target(target.ssh_target)
    return CloudBootstrapTarget(
        ssh_target=ssh_target,
        identity_file=target.identity_file,
        app_path=target.app_path,
        env_path=target.env_path,
        db_path=target.db_path,
        reports_path=target.reports_path,
    )


def _root_target(ssh_target: str) -> str:
    host = ssh_target.split("@", 1)[1] if "@" in ssh_target else ssh_target
    return f"root@{host}"


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _count_mutation(results: list[RemoteProbeResult], name: str) -> int:
    return sum(1 for result in results if result.name == name)


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R19 Cloud Systemd Cutover")
    decision = payload["cutover_decision"]
    pre = payload["preflight_r18"]["runtime_cutover_decision"]
    post = (payload.get("post_cutover_r18") or {}).get("runtime_cutover_decision") or {}
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Recommended action: `{decision['recommended_action']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Preflight R18 status: `{decision['preflight_status']}`",
            f"- Post-cutover R18 status: `{decision['post_cutover_status']}`",
            f"- Expected manual R5 PID: `{payload['expected_r5_pid']}`",
            f"- Preflight current R5 PID: `{pre.get('current_r5_pid')}`",
            f"- Post-cutover current R5 PID: `{post.get('current_r5_pid')}`",
            f"- Codex executed SIGTERM: `{decision['codex_executed_sigterm']}`",
            f"- Codex executed systemd start: `{decision['codex_executed_systemd_start']}`",
            "",
            "## Safety",
            "",
            "- Only the expected R5 PID is eligible for SIGTERM.",
            "- The process command line must contain `phase3bc-r5-crypto-freshness-watch`.",
            "- No paper/live/demo trades are created.",
            "- No live/demo order submit/cancel/replace command is run.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R19 Cutover Detail")
    decision = payload["cutover_decision"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Recommended action: `{decision['recommended_action']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Targets",
            "",
            f"- Inspect target: `{payload['inspect_target']['ssh_target']}`",
            f"- Control target: `{payload['control_target']['ssh_target']}`",
            f"- Service: `{payload['service_name']}`",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["cutover_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(
            f"- `{marker}` `{row['check']}` severity=`{row['severity']}` - {row['detail']}"
        )
    lines.extend(["", "## Remote Cutover Results", ""])
    if payload["remote_cutover_results"]:
        for result in payload["remote_cutover_results"]:
            lines.append(
                f"- `{result['name']}` ok=`{result['ok']}` exit=`{result['exit_code']}` "
                f"duration=`{result['duration_seconds']}`"
            )
    else:
        lines.append("- No mutating remote cutover commands were executed.")
    return "\n".join(lines) + "\n"


def _render_operator_cutover_command(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R19 operator-approved cutover command.",
            "# This stops only the expected manual R5 PID and starts the systemd service.",
            payload["cutover_decision"]["operator_next_command"],
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R19 Next Actions")
    decision = payload["cutover_decision"]
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
            "- Do not use raw `kill` outside this R19 guard.",
            "- Do not start a second R5 watcher manually.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["check", "passed", "severity", "detail"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_remote_results_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
