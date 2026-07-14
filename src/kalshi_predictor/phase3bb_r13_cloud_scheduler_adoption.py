from __future__ import annotations

import csv
import json
import shlex
import time
from collections.abc import Callable
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
    RemoteProbe,
    RemoteProbeResult,
    _json_from_probe,
    _loose_db_writer_state,
    _resolve_target,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R13_VERSION = "phase3bb_r13_cloud_scheduler_adoption_dry_run_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r13")
DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_PER_PROBE_TIMEOUT_SECONDS = 45

RECOMMENDATIONS = {"WAIT", "ADOPT_EXISTING_R5", "STOP_OVERRUN_R5"}


@dataclass(frozen=True)
class Phase3BBR13CloudSchedulerAdoptionArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


ProbeRunner = Callable[[RemoteProbe, CloudBootstrapTarget], RemoteProbeResult]


def write_phase3bb_r13_cloud_scheduler_adoption_report(
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
    expected_r5_pid: int | None = None,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR13CloudSchedulerAdoptionArtifacts:
    payload = build_phase3bb_r13_cloud_scheduler_adoption(
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
        expected_r5_pid=expected_r5_pid,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_scheduler_adoption.md"
    json_path = output_dir / "cloud_scheduler_adoption.json"
    probe_csv_path = output_dir / "remote_probe_results.csv"
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
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR13CloudSchedulerAdoptionArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r13_cloud_scheduler_adoption(
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
    expected_r5_pid: int | None = None,
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
        "command": "kalshi-bot phase3bb-r13-cloud-scheduler-adoption",
        "argv": command_args or [],
    }
    r11_context = _read_json(reports_dir / "phase3bb_r11" / "codex_cloud_context.json")
    r12_context = _read_json(
        reports_dir / "phase3bb_r12" / "cloud_bootstrap_verification.json"
    )
    target = _resolve_target(
        r11_context,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
    )
    expected_pid = expected_r5_pid or _expected_pid_from_r12(r12_context)
    runner = probe_runner or _run_ssh_probe
    probes = _build_remote_probes(
        target,
        expected_r5_pid=expected_pid,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    results = [runner(probe, target) for probe in probes]
    duration = round(time.monotonic() - started, 3)
    parsed = _parse_probe_outputs(results)
    decision = _decide_adoption(parsed, expected_pid=expected_pid)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
        "remote_commands_executed": len(results),
        "remote_report_writes_only": True,
        "remote_db_writes_performed": 0,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "guarded_stop_command_written_only": decision["recommendation"] == "STOP_OVERRUN_R5",
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R13-CLOUD-SCHEDULER-ADOPTION-DRY-RUN",
        "phase_version": PHASE3BB_R13_VERSION,
        "mode": "PAPER_READ_ONLY_REMOTE_SCHEDULER_ADOPTION_DRY_RUN",
        "reports_dir": str(reports_dir),
        "r11_context_available": bool(r11_context),
        "r12_context_available": bool(r12_context),
        "cloud_target": {
            "ssh_target": target.ssh_target,
            "identity_file": target.identity_file,
            "app_path": target.app_path,
            "env_path": target.env_path,
            "db_path": target.db_path,
            "reports_path": target.reports_path,
        },
        "expected_r5_pid": expected_pid,
        "remote_probe_duration_seconds": duration,
        "remote_probe_results": [_result_payload(result) for result in results],
        "parsed_remote_state": parsed,
        "adoption_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _expected_pid_from_r12(r12_context: dict[str, Any]) -> int | None:
    parsed = r12_context.get("parsed_remote_state") or {}
    pids = parsed.get("r5_pids") or []
    if not pids:
        writer = parsed.get("db_writer_monitor") or {}
        pid = writer.get("current_writer_pid")
        return int(pid) if pid else None
    try:
        return int(pids[0])
    except (TypeError, ValueError):
        return None


def _build_remote_probes(
    target: CloudBootstrapTarget,
    *,
    expected_r5_pid: int | None,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    app = shlex.quote(target.app_path)
    env = shlex.quote(target.env_path)
    source_env = f"set -a && . {env} && set +a"
    probes = [
        RemoteProbe(
            "r5_status",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-status "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r13_r5_status.out && "
                "cat reports/phase3bc_r5/phase3bc_r5_status.json"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "r5_guard_dry_run",
            (
                f"cd {app} && {source_env} && "
                ".venv/bin/kalshi-bot phase3bc-r5-unattended-guard "
                "--output-dir reports/phase3bc_r5 >/tmp/phase3bb_r13_guard.out && "
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
            "r5_pid_file",
            f"cd {app} && cat reports/phase3bc_r5/phase3bc_r5_unattended_job.pid",
            timeout_seconds,
        ),
    ]
    if expected_r5_pid is not None:
        probes.append(
            RemoteProbe(
                "expected_pid_process",
                (
                    f"if kill -0 {expected_r5_pid} 2>/dev/null; then "
                    f"ps -p {expected_r5_pid} -o pid=,etimes=,cmd=; "
                    "else echo EXPECTED_PID_NOT_RUNNING; exit 3; fi"
                ),
                timeout_seconds,
            )
        )
    return probes


def _parse_probe_outputs(results: list[RemoteProbeResult]) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    r5_status = _json_from_probe(by_name.get("r5_status"))
    guard = _json_from_probe(by_name.get("r5_guard_dry_run"))
    writer = _json_from_probe(by_name.get("db_writer_monitor"))
    if not writer:
        writer = _loose_db_writer_state(by_name.get("db_writer_monitor"))
    process = r5_status.get("process") if isinstance(r5_status, dict) else {}
    guard_status = r5_status.get("guard") if isinstance(r5_status, dict) else {}
    latest_summary = r5_status.get("latest_summary") if isinstance(r5_status, dict) else {}
    pids = list((process or {}).get("phase3bc_r5_pids") or [])
    pid_file_result = by_name.get("r5_pid_file")
    pid_file_text = (pid_file_result.stdout if pid_file_result else "").strip()
    expected_pid_process = by_name.get("expected_pid_process")
    return {
        "r5_status": r5_status,
        "guard_dry_run": guard,
        "db_writer_monitor": writer,
        "r5_running": bool((process or {}).get("phase3bc_r5_process_running")),
        "r5_pids": pids,
        "r5_pid": _to_int_or_none(r5_status.get("pid") if isinstance(r5_status, dict) else None),
        "pid_file_value": _to_int_or_none(pid_file_text),
        "duplicate_r5": len(pids) > 1,
        "guard_status": (guard_status or {}).get("status"),
        "guard_should_stop": bool((guard_status or {}).get("should_stop")),
        "guard_recommended_next_action": (guard_status or {}).get("recommended_next_action"),
        "guard_latest_age_seconds": (guard_status or {}).get("latest_age_seconds"),
        "guard_seconds_until_timeout": (guard_status or {}).get("seconds_until_timeout"),
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
        "writer_pid": writer.get("current_writer_pid") if isinstance(writer, dict) else None,
        "expected_pid_probe_ok": bool(expected_pid_process.ok) if expected_pid_process else None,
    }


def _decide_adoption(parsed: dict[str, Any], *, expected_pid: int | None) -> dict[str, Any]:
    r5_running = bool(parsed.get("r5_running"))
    pids = [int(pid) for pid in parsed.get("r5_pids") or []]
    duplicate_r5 = bool(parsed.get("duplicate_r5"))
    guard_should_stop = bool(parsed.get("guard_should_stop"))
    guard_status = str(parsed.get("guard_status") or "UNKNOWN")
    writer_pid = _to_int_or_none(parsed.get("writer_pid"))
    r5_pid = _to_int_or_none(parsed.get("r5_pid")) or (pids[0] if pids else None)
    pid_file_value = _to_int_or_none(parsed.get("pid_file_value"))
    expected_probe_ok = parsed.get("expected_pid_probe_ok")
    writer_matches_r5 = writer_pid is None or writer_pid == r5_pid
    expected_matches = expected_pid is None or expected_pid == r5_pid

    if duplicate_r5:
        recommendation = "WAIT"
        reason = "Multiple R5 watcher PIDs are present; manual review is required."
    elif not r5_running:
        recommendation = "WAIT"
        reason = "No running R5 watcher is available to adopt."
    elif expected_probe_ok is False:
        recommendation = "WAIT"
        reason = f"Expected R5 PID {expected_pid} is no longer running."
    elif guard_should_stop or guard_status == "OVERRUNNING":
        recommendation = "STOP_OVERRUN_R5"
        reason = "The guarded R5 watcher reports should_stop=true or OVERRUNNING."
    elif not writer_matches_r5:
        recommendation = "WAIT"
        reason = "A writer is active but it does not match the current R5 PID."
    elif not expected_matches:
        recommendation = "WAIT"
        reason = f"Current R5 PID {r5_pid} does not match expected PID {expected_pid}."
    elif pid_file_value is not None and r5_pid is not None and pid_file_value != r5_pid:
        recommendation = "WAIT"
        reason = f"R5 pid file {pid_file_value} does not match discovered PID {r5_pid}."
    else:
        recommendation = "ADOPT_EXISTING_R5"
        reason = "Exactly one healthy guarded R5 watcher is running and can be adopted."

    if recommendation == "STOP_OVERRUN_R5":
        operator_command = (
            "ssh kalshi@159.65.35.72 'cd /opt/kalshi-predictive-bot && "
            "set -a && . /etc/kalshi-bot/kalshi-bot.env && set +a && "
            ".venv/bin/kalshi-bot phase3bc-r5-unattended-guard "
            "--output-dir reports/phase3bc_r5 --stop-overrun'"
        )
    elif recommendation == "ADOPT_EXISTING_R5":
        operator_command = (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        )
    else:
        operator_command = (
            "kalshi-bot phase3bb-r13-cloud-scheduler-adoption "
            "--output-dir reports/phase3bb_r13 --reports-dir reports"
        )
    return {
        "recommendation": recommendation,
        "recommendation_valid": recommendation in RECOMMENDATIONS,
        "primary_reason": reason,
        "operator_next_command": operator_command,
        "expected_pid": expected_pid,
        "current_r5_pid": r5_pid,
        "pid_file_value": pid_file_value,
        "writer_pid": writer_pid,
        "writer_matches_r5": writer_matches_r5,
        "guard_status": guard_status,
        "guard_should_stop": guard_should_stop,
        "duplicate_r5": duplicate_r5,
        "watch_state": parsed.get("watch_state"),
        "next_codex_step": (
            "Phase 3BB-R14 - Cloud Service Plan / Existing R5 Adoption Service Draft"
        ),
    }


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
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


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R13 Cloud Scheduler Adoption Dry Run")
    decision = payload["adoption_decision"]
    parsed = payload["parsed_remote_state"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Recommendation: `{decision['recommendation']}`",
            f"- Primary reason: {decision['primary_reason']}",
            f"- Expected R5 PID: `{decision['expected_pid']}`",
            f"- Current R5 PID: `{decision['current_r5_pid']}`",
            f"- Writer PID: `{decision['writer_pid']}`",
            f"- Writer matches R5: `{decision['writer_matches_r5']}`",
            "",
            "## R5 State",
            "",
            f"- Running: `{parsed.get('r5_running')}`",
            f"- PIDs: `{parsed.get('r5_pids')}`",
            f"- Duplicate R5: `{parsed.get('duplicate_r5')}`",
            f"- Guard status: `{parsed.get('guard_status')}`",
            f"- Guard should stop: `{parsed.get('guard_should_stop')}`",
            f"- Watch state: `{parsed.get('watch_state')}`",
            f"- Positive EV rows: `{parsed.get('positive_ev_rows')}`",
            f"- Paper-ready candidates: `{parsed.get('paper_ready_candidates')}`",
            "",
            "## Safety",
            "",
            "- Dry run only.",
            "- No scheduler was started.",
            "- No service was installed.",
            "- No guarded stop was executed.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Command",
            "",
            f"```bash\n{decision['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R13 Cloud Scheduler Adoption Detail")
    lines.extend(["", "## Adoption Decision", ""])
    lines.append("```json")
    lines.append(json.dumps(payload["adoption_decision"], indent=2, sort_keys=True))
    lines.append("```")
    lines.extend(["", "## Parsed Remote State", ""])
    summary_keys = [
        "r5_running",
        "r5_pids",
        "duplicate_r5",
        "guard_status",
        "guard_should_stop",
        "watch_state",
        "writer_status",
        "writer_safe_to_start_write",
        "writer_pid",
        "paper_ready_candidates",
        "positive_ev_rows",
        "liquidity_actionability_state",
    ]
    for key in summary_keys:
        lines.append(f"- {key}: `{payload['parsed_remote_state'].get(key)}`")
    lines.extend(["", "## Probe Results", ""])
    for result in payload["remote_probe_results"]:
        lines.append(
            f"- `{result['name']}` ok=`{result['ok']}` exit=`{result['exit_code']}` "
            f"duration=`{result['duration_seconds']}`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["adoption_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "# Phase 3BB-R13 dry-run next command.",
            "# Review NEXT_ACTIONS.md before running.",
            command,
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R13 Next Actions")
    decision = payload["adoption_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Recommendation: `{decision['recommendation']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            "## Guardrails",
            "",
            "- Do not install a service in R13.",
            "- Do not start another R5 watcher in R13.",
            "- Do not run the guarded stop command unless a later explicit phase authorizes it.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
