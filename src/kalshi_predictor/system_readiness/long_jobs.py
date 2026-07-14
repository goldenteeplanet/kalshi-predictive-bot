from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from kalshi_predictor import phase3ay
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.utils.time import utc_now

PHASE3AY_OUTPUT_DIR = Path("reports/phase3ay")
POST_REFRESH_COMMANDS = (
    "kalshi-bot phase3ay-status",
    "kalshi-bot paper-settlement-doctor --output-dir reports/paper_settlement_reconciliation",
    "kalshi-bot phase3aa-realize --dry-run --no-sync-settlements",
    (
        "kalshi-bot institutional-dashboard-report --enable-read-only "
        "--output reports/institutional_dashboard.md"
    ),
    "kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb",
    "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2",
    (
        "kalshi-bot phase3bb-r2-general-source-intake "
        "--output-dir reports/phase3bb_r2_sources"
    ),
    (
        "kalshi-bot phase3bb-r2-general-source-evidence "
        "--output-dir reports/phase3bb_r2_sources"
    ),
    (
        "kalshi-bot phase3bb-r2-general-source-availability "
        "--output-dir reports/phase3bb_r2_sources"
    ),
    "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3",
    "kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az --reports-dir reports",
    (
        "kalshi-bot phase-orchestrator --analyze "
        "--output reports/phase_orchestrator.md "
        "--json-output reports/phase_orchestrator.json "
        "--next-prompt prompts/next_phase.md"
    ),
)


def build_long_job_monitor(
    *,
    settings: Settings | None = None,
    output_dir: Path = PHASE3AY_OUTPUT_DIR,
) -> dict[str, Any]:
    """Build a read-only monitor for the active refresh loop and finish hook."""

    resolved = settings or get_settings()
    phase_status = phase3ay.build_phase3ay_status(output_dir=output_dir)
    writer = db_writer_monitor(settings=resolved)
    phase_process = phase_status.get("process") or {}
    phase_guard = phase_status.get("guard") or {}
    phase_running = bool(phase_process.get("phase3ay_process_running"))
    pids = list(phase_process.get("phase3ay_pids") or [])
    active_pid = int(pids[0]) if pids else None
    runtime = _process_runtime(active_pid) if active_pid else {}
    command = runtime.get("command") or (
        "kalshi-bot phase3ay-health-refresh" if active_pid else None
    )
    duration_seconds = _duration_seconds_from_command(command)
    elapsed_seconds = runtime.get("elapsed_seconds")
    remaining_seconds = _remaining_seconds(
        elapsed_seconds=elapsed_seconds,
        duration_seconds=duration_seconds,
    )
    overrun_seconds = _overrun_seconds(
        elapsed_seconds=elapsed_seconds,
        duration_seconds=duration_seconds,
    )
    budget_state = _budget_state(
        phase_running=phase_running,
        elapsed_seconds=elapsed_seconds,
        duration_seconds=duration_seconds,
    )
    generated_at = utc_now()
    expected_finish_at = (
        (generated_at.timestamp() + remaining_seconds)
        if remaining_seconds is not None
        else None
    )
    hook = _post_refresh_hook_status(output_dir=output_dir, phase_running=phase_running)
    progress_percent = _progress_percent(
        elapsed_seconds=elapsed_seconds,
        duration_seconds=duration_seconds,
    )
    return {
        "phase": "LONG_JOB_MONITOR",
        "generated_at": generated_at.isoformat(),
        "read_only": True,
        "paper_only_safety": "PAPER_ONLY_NO_EXCHANGE_WRITES",
        "phase3ay": {
            "status": "RUNNING" if phase_running else "STOPPED",
            "active_pid": active_pid,
            "pids": pids,
            "command": command,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_label": _format_duration(elapsed_seconds),
            "duration_seconds": duration_seconds,
            "duration_label": _format_duration(duration_seconds),
            "remaining_seconds": remaining_seconds,
            "remaining_label": _format_duration(remaining_seconds),
            "overrun_seconds": overrun_seconds,
            "overrun_label": _format_duration(overrun_seconds),
            "budget_state": budget_state,
            "budget_label": _budget_label(
                budget_state=budget_state,
                remaining_seconds=remaining_seconds,
                overrun_seconds=overrun_seconds,
            ),
            "progress_percent": progress_percent,
            "expected_finish_timestamp": expected_finish_at,
            "latest_report_generated_at": phase_status.get("latest_report_generated_at"),
            "latest_status": phase_status.get("latest_status") or "NO_REPORT",
            "latest_summary": phase_status.get("latest_summary") or {},
            "history_rows": phase_status.get("history_rows") or 0,
            "guard_status": phase_guard.get("status") or "UNKNOWN",
            "guard_should_stop": bool(phase_guard.get("should_stop")),
            "guard_seconds_until_timeout": phase_guard.get("seconds_until_timeout"),
            "guard_safe_to_resume": bool(phase_guard.get("safe_to_resume")),
            "guard_resume_command": phase_guard.get("resume_command"),
            "stdout_path": (phase_status.get("logs") or {}).get("stdout_path"),
            "stdout_bytes": (phase_status.get("logs") or {}).get("stdout_bytes"),
            "stderr_path": (phase_status.get("logs") or {}).get("stderr_path"),
            "stderr_bytes": (phase_status.get("logs") or {}).get("stderr_bytes"),
            "recommended_next_action": phase_guard.get("recommended_next_action")
            or phase_status.get("recommended_next_action"),
        },
        "db_writer_monitor": writer,
        "post_refresh_hook": hook,
        "safety": {
            "live_trading_authorized": False,
            "exchange_writes": False,
            "starts_heavy_jobs": False,
            "reads_report_files_only": True,
        },
        "recommended_next_action": _recommended_next_action(
            phase_running=phase_running,
            hook=hook,
            writer=writer,
        ),
    }


def _process_runtime(pid: int | None) -> dict[str, Any]:
    if pid is None:
        return {}
    pid_dir = Path("/proc") / str(pid)
    command = _process_command(pid_dir)
    elapsed_seconds = _process_elapsed_seconds(pid)
    return {
        "pid": pid,
        "running": _pid_running(pid),
        "command": command,
        "elapsed_seconds": elapsed_seconds,
    }


def _process_command(pid_dir: Path) -> str | None:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return None
    command = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    return command or None


def _process_elapsed_seconds(pid: int) -> int | None:
    if os.name == "posix":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "etimes="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None
    return None


def _duration_seconds_from_command(command: str | None) -> int | None:
    if not command:
        return None
    hours = _option_value(command, "duration-hours")
    if hours is not None:
        try:
            return int(float(hours) * 3600)
        except ValueError:
            return None
    cycles = _option_value(command, "cycles")
    interval = _option_value(command, "interval-seconds")
    if cycles is None or interval is None:
        return None
    try:
        return int(float(cycles) * float(interval))
    except ValueError:
        return None


def _option_value(command: str, name: str) -> str | None:
    pattern = rf"--{re.escape(name)}(?:=|\s+)([^\s]+)"
    match = re.search(pattern, command)
    return match.group(1) if match else None


def _remaining_seconds(
    *,
    elapsed_seconds: int | None,
    duration_seconds: int | None,
) -> int | None:
    if elapsed_seconds is None or duration_seconds is None:
        return None
    return max(0, duration_seconds - elapsed_seconds)


def _overrun_seconds(
    *,
    elapsed_seconds: int | None,
    duration_seconds: int | None,
) -> int | None:
    if elapsed_seconds is None or duration_seconds is None:
        return None
    return max(0, elapsed_seconds - duration_seconds)


def _budget_state(
    *,
    phase_running: bool,
    elapsed_seconds: int | None,
    duration_seconds: int | None,
) -> str:
    if elapsed_seconds is None or duration_seconds is None:
        return "UNKNOWN"
    if phase_running and elapsed_seconds > duration_seconds:
        return "OVERRUNNING"
    if phase_running:
        return "WITHIN_BUDGET"
    if elapsed_seconds > duration_seconds:
        return "FINISHED_AFTER_BUDGET"
    return "FINISHED_WITHIN_BUDGET"


def _budget_label(
    *,
    budget_state: str,
    remaining_seconds: int | None,
    overrun_seconds: int | None,
) -> str:
    if budget_state == "OVERRUNNING":
        return f"Over by {_format_duration(overrun_seconds) or 'unknown'}"
    if budget_state == "WITHIN_BUDGET":
        return f"{_format_duration(remaining_seconds) or 'unknown'} remaining"
    if budget_state == "FINISHED_AFTER_BUDGET":
        return f"Finished after budget by {_format_duration(overrun_seconds) or 'unknown'}"
    if budget_state == "FINISHED_WITHIN_BUDGET":
        return "Finished within budget"
    return "Budget unknown"


def _progress_percent(
    *,
    elapsed_seconds: int | None,
    duration_seconds: int | None,
) -> float | None:
    if elapsed_seconds is None or not duration_seconds:
        return None
    return round(min(100.0, max(0.0, elapsed_seconds / duration_seconds * 100)), 1)


def _post_refresh_hook_status(*, output_dir: Path, phase_running: bool) -> dict[str, Any]:
    pid_path = output_dir / "post_refresh_watch.pid"
    log_path = output_dir / "post_refresh_watch.log"
    pid = _read_pid(pid_path)
    running = _pid_running(pid) if pid is not None else False
    log_tail = _tail_text(log_path, max_lines=12)
    last_line = next((line for line in reversed(log_tail) if line.strip()), None)
    if running:
        status = "WAITING_FOR_REFRESH" if phase_running else "RUNNING_DIAGNOSTICS"
    elif last_line and "post_refresh_diagnostics_done" in last_line:
        status = "COMPLETE"
    elif pid is None and not log_tail:
        status = "NOT_CONFIGURED"
    else:
        status = "STOPPED"
    return {
        "status": status,
        "pid": pid,
        "running": running,
        "pid_path": str(pid_path),
        "log_path": str(log_path),
        "last_log_line": last_line,
        "log_tail": log_tail,
        "planned_commands": list(POST_REFRESH_COMMANDS),
    }


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _pid_running(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return not _pid_is_zombie(pid)
    return False


def _pid_is_zombie(pid: int) -> bool:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return False
    parts = stat.split()
    return len(parts) > 2 and parts[2] == "Z"


def _tail_text(path: Path, *, max_lines: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    days, remainder = divmod(max(0, int(seconds)), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _recommended_next_action(
    *,
    phase_running: bool,
    hook: dict[str, Any],
    writer: dict[str, Any],
) -> str:
    if phase_running and hook["running"]:
        return "Leave the refresh and post-refresh watcher running; avoid heavy producer jobs."
    if phase_running:
        return (
            "Refresh is running; configure or restart the post-refresh watcher "
            "before stepping away."
        )
    if hook["status"] == "COMPLETE":
        return (
            "Refresh and post-refresh diagnostics are complete; review Phase 3AZ "
            "and next_phase prompt."
        )
    if writer.get("status") == "WRITER_ACTIVE":
        return "A DB writer is active; wait before starting any repair or producer job."
    return "No refresh is running. Review latest diagnostics before starting the next write job."
