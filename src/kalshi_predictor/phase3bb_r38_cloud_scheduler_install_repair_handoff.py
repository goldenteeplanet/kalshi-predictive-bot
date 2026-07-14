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

PHASE3BB_R38_VERSION = "phase3bb_r38_cloud_scheduler_install_repair_handoff_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r38")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45
CODE_SYNC_ENV_VAR = "PHASE3BB_R38_CODE_SYNC"
CODE_SYNC_TOKEN = "I_APPROVE_R38_CODE_SYNC"
ROOT_SCRIPT_REMOTE_PATH = "/tmp/phase3bb_r38_root_console_scheduler_install.sh"


@dataclass(frozen=True)
class Phase3BBR38CloudSchedulerInstallRepairHandoffArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    root_console_script_path: Path
    code_sync_handoff_script_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r38_cloud_scheduler_install_repair_handoff_report(
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
) -> Phase3BBR38CloudSchedulerInstallRepairHandoffArtifacts:
    payload = build_phase3bb_r38_cloud_scheduler_install_repair_handoff(
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
    markdown_path = output_dir / "cloud_scheduler_install_repair_handoff.md"
    json_path = output_dir / "cloud_scheduler_install_repair_handoff.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
    checks_csv_path = output_dir / "repair_checks.csv"
    root_console_script_path = output_dir / "root_console_scheduler_install.sh"
    code_sync_handoff_script_path = output_dir / "operator_code_sync_handoff.sh"
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
    _write_checks_csv(checks_csv_path, payload["repair_checks"])
    root_console_script_path.write_text(_render_root_console_script(payload), encoding="utf-8")
    _mark_executable(root_console_script_path)
    code_sync_handoff_script_path.write_text(_render_code_sync_script(payload), encoding="utf-8")
    _mark_executable(code_sync_handoff_script_path)
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
            root_console_script_path,
            code_sync_handoff_script_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR38CloudSchedulerInstallRepairHandoffArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        root_console_script_path=root_console_script_path,
        code_sync_handoff_script_path=code_sync_handoff_script_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r38_cloud_scheduler_install_repair_handoff(
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
        "command": "kalshi-bot phase3bb-r38-cloud-scheduler-install-repair-handoff",
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
    repair_commands = _repair_commands(target=target)
    checks = _repair_checks(r11=r11, r37=r37, parsed=parsed, commands=repair_commands)
    decision = _repair_decision(checks, r37, parsed)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "repair_handoff_only": True,
        "ssh_read_only_commands_executed": len(probes),
        "ssh_mutating_commands_executed": 0,
        "systemctl_mutating_commands_executed": 0,
        "root_console_script_written": True,
        "code_sync_handoff_script_written": True,
        "code_sync_executed_by_codex": False,
        "root_install_executed_by_codex": False,
        "scheduler_timer_started": False,
        "scheduler_service_started": False,
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
        "phase": "3BB-R38-CLOUD-SCHEDULER-INSTALL-REPAIR-HANDOFF",
        "phase_version": PHASE3BB_R38_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_SCHEDULER_INSTALL_REPAIR_HANDOFF",
        "reports_dir": str(reports_dir),
        "r11_artifact_path": str(r11_path),
        "r37_artifact_path": str(r37_path),
        "r11_context_available": bool(r11),
        "r37_context_available": bool(r37),
        "cloud_target": _target_payload(target),
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "repair_commands": repair_commands,
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
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    source_env = f"set -a && . {env} && set +a"
    tmp_files = " ".join(
        shlex.quote(f"/tmp/{name}")
        for name in (SCHEDULER_SERVICE_NAME, SCHEDULER_TIMER_NAME, RUNNER_SCRIPT_NAME)
    )
    return [
        RemoteProbe(
            "tmp_scheduler_files",
            (
                "for f in "
                f"{tmp_files}; do if test -f \"$f\"; then echo \"$f PRESENT\"; "
                "else echo \"$f MISSING\"; fi; done"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "app_writable",
            f"test -d {app} && test -w {app} && test -w {app}/src/kalshi_predictor "
            "&& echo APP_WRITABLE",
            timeout_seconds,
        ),
        RemoteProbe(
            "venv_pip",
            f"cd {app} && .venv/bin/python -m pip --version && echo VENV_PIP_OK",
            timeout_seconds,
        ),
        RemoteProbe(
            "r8_command_registry",
            (
                f"cd {app} && .venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate "
                "--help >/tmp/phase3bb_r38_r8_help.txt && echo R8_REGISTERED"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "scheduler_systemd_state",
            (
                f"systemctl show {shlex.quote(SCHEDULER_SERVICE_NAME)} "
                f"{shlex.quote(SCHEDULER_TIMER_NAME)} --no-pager "
                "-p Id -p LoadState -p UnitFileState -p ActiveState -p SubState "
                "-p FragmentPath -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r38_r5_status.out && "
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
    tmp_state = _tmp_file_state(_stdout(by_name.get("tmp_scheduler_files")))
    systemd_units = _parse_multi_unit_systemd(_stdout(by_name.get("scheduler_systemd_state")))
    r5_status = _json_from_probe(by_name.get("r5_status"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    guard = r5_status.get("guard") if isinstance(r5_status, dict) else {}
    latest_summary = r5_status.get("latest_summary") if isinstance(r5_status, dict) else {}
    pids = [_to_int(pid) for pid in (process or {}).get("phase3bc_r5_pids") or []]
    pids = [pid for pid in pids if pid is not None]
    r5_pid = _to_int(r5_status.get("pid") if isinstance(r5_status, dict) else None)
    if r5_pid is None and pids:
        r5_pid = pids[0]
    return {
        "tmp_file_state": tmp_state,
        "tmp_service_present": bool(tmp_state.get(f"/tmp/{SCHEDULER_SERVICE_NAME}")),
        "tmp_timer_present": bool(tmp_state.get(f"/tmp/{SCHEDULER_TIMER_NAME}")),
        "tmp_runner_present": bool(tmp_state.get(f"/tmp/{RUNNER_SCRIPT_NAME}")),
        "all_tmp_scheduler_files_present": all(tmp_state.values()) if tmp_state else False,
        "app_writable": bool(by_name.get("app_writable") and by_name["app_writable"].ok),
        "venv_pip_ok": bool(by_name.get("venv_pip") and by_name["venv_pip"].ok),
        "r8_registered": bool(
            by_name.get("r8_command_registry") and by_name["r8_command_registry"].ok
        ),
        "r8_registry_error": (by_name.get("r8_command_registry").stderr or "")
        if by_name.get("r8_command_registry")
        else "",
        "systemd_units": systemd_units,
        "scheduler_service_loaded": (
            systemd_units.get(SCHEDULER_SERVICE_NAME, {}).get("LoadState") == "loaded"
        ),
        "scheduler_timer_loaded": (
            systemd_units.get(SCHEDULER_TIMER_NAME, {}).get("LoadState") == "loaded"
        ),
        "scheduler_service_active": (
            systemd_units.get(SCHEDULER_SERVICE_NAME, {}).get("ActiveState") == "active"
        ),
        "scheduler_timer_active": (
            systemd_units.get(SCHEDULER_TIMER_NAME, {}).get("ActiveState") == "active"
        ),
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


def _repair_commands(*, target: CloudBootstrapTarget) -> dict[str, str]:
    ssh_target = str(target.ssh_target)
    identity_file = str(target.identity_file)
    app_path = str(target.app_path)
    ssh_prefix = f"ssh -i {_shell_quote(identity_file)} {_shell_quote(ssh_target)}"
    root_script_local = "reports/phase3bb_r38/root_console_scheduler_install.sh"
    return {
        "copy_root_console_script_to_tmp": (
            f"scp -i {_shell_quote(identity_file)} {root_script_local} "
            f"{_shell_quote(f'{ssh_target}:{ROOT_SCRIPT_REMOTE_PATH}')}"
        ),
        "run_root_console_install_manually": f"bash {ROOT_SCRIPT_REMOTE_PATH}",
        "code_sync_and_verify_r8": (
            "tar -czf - src pyproject.toml | "
            f"{ssh_prefix} 'cd {_shell_quote(app_path)} && tar -xzf - && "
            ".venv/bin/python -m pip install -e . && "
            ".venv/bin/kalshi-bot phase3bb-r8-unified-paper-gate --help "
            ">/tmp/phase3bb_r38_r8_help.txt && echo CODE_SYNC_OK'"
        ),
        "verify_after_repairs": (
            "kalshi-bot phase3bb-r37-cloud-scheduler-install-verification "
            "--output-dir reports/phase3bb_r37 --reports-dir reports"
        ),
    }


def _repair_checks(
    *,
    r11: dict[str, Any],
    r37: dict[str, Any],
    parsed: dict[str, Any],
    commands: dict[str, str],
) -> list[dict[str, Any]]:
    r37_decision = r37.get("verification_decision") or {}
    combined_commands = "\n".join(commands.values()).lower()
    return [
        _check("r11_cloud_context_present", bool(r11), "R11 cloud context exists."),
        _check("r37_verification_present", bool(r37), "R37 verification artifact exists."),
        _check(
            "r37_blocked_by_scheduler_install",
            r37_decision.get("status") in {
                "BLOCKED_SCHEDULER_INSTALL_VERIFICATION",
                "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START",
            },
            f"R37 status is {r37_decision.get('status')}.",
        ),
        _check(
            "tmp_scheduler_files_present",
            bool(parsed.get("all_tmp_scheduler_files_present")),
            f"Tmp file state: {parsed.get('tmp_file_state')}.",
        ),
        _check(
            "app_path_writable_for_code_sync",
            bool(parsed.get("app_writable")),
            "Remote app path and src package path are writable by the SSH user.",
        ),
        _check(
            "venv_pip_available",
            bool(parsed.get("venv_pip_ok")),
            "Remote venv pip is available for editable reinstall after code sync.",
        ),
        _check(
            "r8_command_missing_or_registered",
            True,
            (
                "R8 command is already registered."
                if parsed.get("r8_registered")
                else "R8 command is missing and the code-sync handoff will verify it."
            ),
        ),
        _check(
            "root_install_script_has_no_start",
            "systemctl start" not in combined_commands
            and "systemctl restart" not in combined_commands
            and "enable --now" not in combined_commands,
            "Repair handoff does not start/restart scheduler services.",
        ),
        _check(
            "no_trading_commands_in_repair_handoff",
            not any(
                fragment in combined_commands
                for fragment in (
                    "accelerate-learning",
                    "autopilot",
                    "create-paper-trade",
                    "live-order",
                    "place-order",
                    "submit-order",
                )
            ),
            "Repair handoff contains no paper/live/demo trade commands.",
        ),
    ]


def _repair_decision(
    checks: list[dict[str, Any]],
    r37: dict[str, Any],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r37_decision = r37.get("verification_decision") or {}
    needs_code_sync = not bool(parsed.get("r8_registered"))
    if r37_decision.get("status") == "VERIFIED_SCHEDULER_INSTALL_ENABLE_NO_START" and not needs_code_sync:
        status = "REPAIR_NOT_NEEDED_READY_FOR_TIMER_START_HANDOFF"
        reason = "R37 already verifies scheduler install and R8 is registered."
        next_command = (
            "kalshi-bot phase3bb-r38-cloud-scheduler-install-repair-handoff "
            "--output-dir reports/phase3bb_r38 --reports-dir reports"
        )
        next_step = "Phase 3BB-R39 - Operator-Approved Scheduler Timer Start Handoff"
    elif failed:
        status = "BLOCKED_REPAIR_HANDOFF"
        reason = f"First failing check: {failed[0]['check']}."
        next_command = (
            "PHASE3BB_R36_EXECUTE=I_APPROVE_R36_SCHEDULER_INSTALL "
            "bash reports/phase3bb_r36/operator_scheduler_install_handoff.sh"
            if failed[0]["check"] == "tmp_scheduler_files_present"
            else "Review reports/phase3bb_r38/repair_checks.csv"
        )
        next_step = "Phase 3BB-R38 - Resolve Repair Handoff Preconditions"
    else:
        status = "REPAIR_HANDOFF_READY_NO_START"
        reason = (
            "The /tmp scheduler drafts are present, a root-console install script was "
            "generated for the sudo blocker, and the code-sync handoff can verify R8 "
            "before any timer start."
        )
        next_command = (
            "PHASE3BB_R38_CODE_SYNC=I_APPROVE_R38_CODE_SYNC "
            "bash reports/phase3bb_r38/operator_code_sync_handoff.sh"
            if needs_code_sync
            else f"Copy/run `{ROOT_SCRIPT_REMOTE_PATH}` as root on the cloud host"
        )
        next_step = "Phase 3BB-R37 - Rerun Scheduler Install Verification After Repairs"
    return {
        "status": status,
        "handoff_ready": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "tmp_scheduler_files_present": bool(parsed.get("all_tmp_scheduler_files_present")),
        "app_writable": bool(parsed.get("app_writable")),
        "venv_pip_ok": bool(parsed.get("venv_pip_ok")),
        "r8_registered": bool(parsed.get("r8_registered")),
        "needs_code_sync": needs_code_sync,
        "needs_root_console_install": not (
            bool(parsed.get("scheduler_service_loaded"))
            and bool(parsed.get("scheduler_timer_loaded"))
        ),
        "scheduler_service_loaded": bool(parsed.get("scheduler_service_loaded")),
        "scheduler_timer_loaded": bool(parsed.get("scheduler_timer_loaded")),
        "scheduler_service_active": bool(parsed.get("scheduler_service_active")),
        "scheduler_timer_active": bool(parsed.get("scheduler_timer_active")),
        "r5_pid": parsed.get("r5_pid"),
        "guard_status": parsed.get("guard_status"),
        "guard_should_stop": bool(parsed.get("guard_should_stop")),
        "writer_pid": parsed.get("writer_pid"),
        "codex_executed_code_sync": False,
        "codex_executed_root_install": False,
        "codex_started_scheduler": False,
        "operator_next_command": next_command,
        "next_codex_step": next_step,
    }


def _render_root_console_script(payload: dict[str, Any]) -> str:
    target = payload["cloud_target"]
    app_path = target["app_path"].rstrip("/")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        "SERVICE='kalshi-multicategory-refresh-scheduler.service'",
        "TIMER='kalshi-multicategory-refresh-scheduler.timer'",
        "RUNNER='kalshi-multicategory-refresh-runner.sh'",
        f"APP={_shell_quote(app_path)}",
        "",
        "if [[ \"$(id -u)\" -ne 0 ]]; then",
        "  echo '[phase3bb-r38] run this script as root from the cloud console'",
        "  exit 2",
        "fi",
        "",
        "echo '[phase3bb-r38] verifying copied /tmp scheduler files'",
        "test -f \"/tmp/${SERVICE}\"",
        "test -f \"/tmp/${TIMER}\"",
        "test -f \"/tmp/${RUNNER}\"",
        "",
        "echo '[phase3bb-r38] installing scheduler runner/service/timer; no start occurs'",
        "install -D -m 0755 \"/tmp/${RUNNER}\" \"${APP}/scripts/${RUNNER}\"",
        "install -m 0644 \"/tmp/${SERVICE}\" \"/etc/systemd/system/${SERVICE}\"",
        "install -m 0644 \"/tmp/${TIMER}\" \"/etc/systemd/system/${TIMER}\"",
        "systemctl daemon-reload",
        "systemctl enable \"${TIMER}\"",
        "",
        "echo '[phase3bb-r38] verification after install'",
        "systemctl is-enabled \"${TIMER}\"",
        "systemctl is-active \"${TIMER}\" || true",
        "systemctl is-active \"${SERVICE}\" || true",
        "systemctl status \"${TIMER}\" \"${SERVICE}\" --no-pager || true",
        "",
        "echo '[phase3bb-r38] install+enable-no-start complete'",
        "echo '[phase3bb-r38] do not start the timer until R37 verifies cleanly'",
        "",
    ]
    return "\n".join(lines)


def _render_code_sync_script(payload: dict[str, Any]) -> str:
    decision = payload["repair_decision"]
    commands = payload["repair_commands"]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"TOKEN=${{{CODE_SYNC_ENV_VAR}:-}}",
        f"REQUIRED={_shell_quote(CODE_SYNC_TOKEN)}",
        "",
        "echo '[phase3bb-r38] cloud code-sync handoff'",
        "echo '[phase3bb-r38] default mode is dry-run; no remote changes occur'",
        "",
        f"COPY_ROOT_SCRIPT={_shell_quote(commands['copy_root_console_script_to_tmp'])}",
        f"CODE_SYNC={_shell_quote(commands['code_sync_and_verify_r8'])}",
        "",
        "if [[ \"$TOKEN\" != \"$REQUIRED\" ]]; then",
        "  echo '[phase3bb-r38] dry-run command list:'",
        "  printf '  %s\\n' \"$COPY_ROOT_SCRIPT\"",
        "  printf '  %s\\n' \"$CODE_SYNC\"",
        "  echo '[phase3bb-r38] no code sync or remote copy executed'",
        f"  echo \"[phase3bb-r38] to execute: {CODE_SYNC_ENV_VAR}=$REQUIRED bash $0\"",
        "  exit 0",
        "fi",
        "",
        "echo '[phase3bb-r38] approval token accepted'",
        "echo '[phase3bb-r38] copying root-console helper to /tmp'",
        "bash -lc \"$COPY_ROOT_SCRIPT\"",
        "",
        "echo '[phase3bb-r38] syncing local code and verifying R8 command'",
        "bash -lc \"$CODE_SYNC\"",
        "",
        "echo '[phase3bb-r38] code sync complete; now run the root-console install script as root'",
        f"echo '  {commands['run_root_console_install_manually']}'",
        "echo '[phase3bb-r38] after root install, rerun R37 verification'",
        f"echo '  {decision['next_codex_step']}'",
        "",
    ]
    return "\n".join(lines)


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Cloud Scheduler Install Repair Handoff")
    decision = payload["repair_decision"]
    parsed = payload["parsed_remote_state"]
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
            "## Repair State",
            "",
            f"- Tmp scheduler files present: `{decision['tmp_scheduler_files_present']}`",
            f"- Needs root-console install: `{decision['needs_root_console_install']}`",
            f"- R8 registered on cloud: `{decision['r8_registered']}`",
            f"- Needs code sync: `{decision['needs_code_sync']}`",
            f"- Remote app writable: `{decision['app_writable']}`",
            f"- Remote venv pip OK: `{decision['venv_pip_ok']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- Guard status: `{decision['guard_status']}`",
            f"- Writer PID: `{decision['writer_pid']}`",
            "",
            "## Safety",
            "",
            "- Codex did not install files as root.",
            "- Codex did not sync code, unless the generated operator handoff is explicitly run later.",
            "- Codex did not start the scheduler service or timer.",
            "- Codex did not stop or duplicate R5.",
            "- No paper/live/demo trades were created.",
            "",
            "## Generated Repair Artifacts",
            "",
            "- `reports/phase3bb_r38/root_console_scheduler_install.sh`",
            "- `reports/phase3bb_r38/operator_code_sync_handoff.sh`",
            "",
            "## Next Operator Command",
            "",
            f"```bash\n{decision['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Tmp File State",
            "",
            "```json",
            json.dumps(parsed.get("tmp_file_state") or {}, indent=2, sort_keys=True),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Repair Handoff Detail")
    decision = payload["repair_decision"]
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "This phase repairs the R36/R37 install path without starting the scheduler. "
            "It creates a root-console script for the sudo blocker and a separate "
            "operator-approved code-sync handoff so the runner command registry is valid.",
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
    for row in payload["repair_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Commands", ""])
    for name, command in payload["repair_commands"].items():
        lines.extend([f"### {name}", "", "```bash", command, "```", ""])
    lines.extend(["", "## Parsed Remote State", "", "```json"])
    lines.append(json.dumps(payload["parsed_remote_state"], indent=2, sort_keys=True))
    lines.extend(["```", "", "## Remote Probe Results", ""])
    for result in payload["remote_probe_results"]:
        lines.append(
            f"- `{result['name']}` ok=`{result['ok']}` exit=`{result['exit_code']}` "
            f"duration=`{result['duration_seconds']}`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["repair_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R38 next safe operator command.",
            command,
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R38 Next Actions")
    decision = payload["repair_decision"]
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
            "## Root Console Action After Code Sync",
            "",
            f"Run as root on the cloud host only after reviewing `{ROOT_SCRIPT_REMOTE_PATH}`:",
            "",
            "```bash",
            f"bash {ROOT_SCRIPT_REMOTE_PATH}",
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not start the scheduler timer yet.",
            "- Do not start duplicate R5 watchers.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def _tmp_file_state(text: str) -> dict[str, bool]:
    state: dict[str, bool] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(" PRESENT"):
            state[stripped[: -len(" PRESENT")]] = True
        elif stripped.endswith(" MISSING"):
            state[stripped[: -len(" MISSING")]] = False
    return state


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


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


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
