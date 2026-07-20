from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.backend import (
    backend_label,
    database_url_from_settings,
    redact_database_url,
    sqlite_path_from_url,
)
from kalshi_predictor.phase3au import load_latest_long_job_status

SQLITE_LOCKED_FRAGMENTS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)

LIKELY_WRITER_MARKERS = (
    "settlement-watch",
    "sync-settlements",
    "tonight-run",
    "overnight-run",
    "learning-once",
    "learning-run",
    "ingest-",
    "build-",
    "link-",
    "forecast",
    "paper-pnl",
    "model-link-repair",
    "model-feature-repair",
    "phase3bb-r43-single-writer-coordinator",
    "single-writer-coordinator",
)


def is_database_locked_error(exc: BaseException) -> bool:
    """Return true for SQLite lock errors, including SQLAlchemy-wrapped errors."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if any(fragment in message for fragment in SQLITE_LOCKED_FRAGMENTS):
            return True
        current = getattr(current, "orig", None)
    return False


def sqlite_lock_diagnostics(
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    url = db_url or database_url_from_settings(resolved_settings)
    sqlite_path = sqlite_path_from_url(url)
    payload: dict[str, Any] = {
        "backend": backend_label(resolved_settings, db_url=url),
        "database_url": redact_database_url(url),
        "database_path": str(sqlite_path) if sqlite_path else None,
        "target_files": [],
        "holders": [],
        "writer_holders": [],
        "scan_method": "not_applicable",
        "safe_to_write": True,
        "status": "CLEAR",
        "next_action": "No local SQLite lock diagnostics are needed for this backend.",
    }

    if sqlite_path is None or str(sqlite_path) == ":memory:":
        return payload

    target_files = _sqlite_target_files(sqlite_path)
    holders, scan_method = _scan_proc_file_holders(target_files)
    writer_holders = [holder for holder in holders if holder["likely_writer"]]
    payload.update(
        {
            "target_files": [str(path) for path in target_files],
            "holders": holders,
            "writer_holders": writer_holders,
            "scan_method": scan_method,
            "safe_to_write": not writer_holders,
            "status": _lock_status(holders, writer_holders, scan_method),
            "next_action": _next_action(holders, writer_holders, scan_method),
        }
    )
    return payload


def db_writer_monitor(
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a UI/CLI-friendly summary of the active SQLite writer job."""

    payload = diagnostics or sqlite_lock_diagnostics(settings=settings, db_url=db_url)
    writer_holders = list(payload.get("writer_holders") or [])
    current_writer = writer_holders[0] if writer_holders else None
    safe_to_start = bool(payload.get("safe_to_write", True))
    recommended_command = _recommended_next_command(current_writer)
    long_job_status = load_latest_long_job_status()
    heartbeat = long_job_status.get("heartbeat") or {}
    heartbeat_pid = heartbeat.get("pid")
    current_writer_pid = current_writer.get("pid") if current_writer else None
    heartbeat_matches_current_writer = _same_pid(heartbeat_pid, current_writer_pid)
    heartbeat_pid_alive = _pid_exists(heartbeat_pid)
    heartbeat_is_stale_orphan = (
        bool(heartbeat)
        and str(long_job_status.get("status") or "").upper() == "STALE"
        and not heartbeat_matches_current_writer
        and not heartbeat_pid_alive
    )
    heartbeat_display_status = (
        "STALE_ORPHANED" if heartbeat_is_stale_orphan else long_job_status.get("status")
    )
    return {
        "status": "WRITER_ACTIVE" if current_writer else payload.get("status", "CLEAR"),
        "diagnostics_status": payload.get("status", "CLEAR"),
        "backend": payload.get("backend"),
        "database_url": payload.get("database_url"),
        "database_path": payload.get("database_path"),
        "scan_method": payload.get("scan_method", "unknown"),
        "safe_to_start_write": safe_to_start,
        "current_writer": current_writer,
        "current_writer_pid": current_writer.get("pid") if current_writer else None,
        "current_writer_command": current_writer.get("command") if current_writer else None,
        "current_writer_elapsed_seconds": (
            current_writer.get("elapsed_seconds") if current_writer else None
        ),
        "current_writer_elapsed": current_writer.get("elapsed") if current_writer else None,
        "writer_count": len(writer_holders),
        "holder_count": len(payload.get("holders") or []),
        "recommended_next_command_after_finish": recommended_command,
        "recommended_next_command": recommended_command,
        "recommended_next_action": _monitor_next_action(
            current_writer=current_writer,
            safe_to_start_write=safe_to_start,
            recommended_command=recommended_command,
        ),
        "long_job_status": long_job_status,
        "long_job_heartbeat_status": heartbeat_display_status,
        "long_job_heartbeat_raw_status": long_job_status.get("status"),
        "long_job_heartbeat_display_status": heartbeat_display_status,
        "long_job_heartbeat_matches_current_writer": heartbeat_matches_current_writer,
        "long_job_heartbeat_pid_alive": heartbeat_pid_alive,
        "long_job_heartbeat_stale_orphaned": heartbeat_is_stale_orphan,
        "long_job_heartbeat_age": long_job_status.get("heartbeat_age"),
        "long_job_stage": heartbeat.get("stage"),
        "long_job_processed": heartbeat.get("processed"),
        "long_job_total": heartbeat.get("total"),
        "next_action": payload.get("next_action"),
    }


def friendly_database_locked_message(
    *,
    settings: Settings | None = None,
    db_url: str | None = None,
) -> str:
    diagnostics = sqlite_lock_diagnostics(settings=settings, db_url=db_url)
    monitor = db_writer_monitor(diagnostics=diagnostics)
    lines = [
        "Database is busy. Another bot process is using SQLite.",
        f"Database: {diagnostics.get('database_path') or diagnostics['database_url']}",
        f"Status: {diagnostics['status']}",
        f"Next action: {diagnostics['next_action']}",
    ]
    holders = diagnostics.get("holders") or []
    if holders:
        lines.append("Open DB holders:")
        for holder in holders[:8]:
            marker = " writer" if holder.get("likely_writer") else ""
            elapsed = holder.get("elapsed") or "n/a"
            files = ", ".join(Path(path).name for path in holder.get("open_files", []))
            lines.append(
                f"- pid {holder['pid']}{marker}, elapsed {elapsed}: "
                f"{holder['command']} ({files})"
            )
    else:
        lines.append("No DB file holders were visible to the process scanner.")
    lines.append(
        "Recommended next command after finish: "
        f"{monitor['recommended_next_command_after_finish']}"
    )
    lines.append("Wait for settlement/learning jobs to finish, then retry the command.")
    return "\n".join(lines)


def _sqlite_target_files(path: Path) -> list[Path]:
    resolved = path.expanduser().resolve()
    return [resolved, Path(f"{resolved}-wal"), Path(f"{resolved}-shm")]


def _scan_proc_file_holders(target_files: list[Path]) -> tuple[list[dict[str, Any]], str]:
    proc = Path("/proc")
    if not proc.exists():
        return [], "procfs_unavailable"

    target_strings = {_normalize_path(path) for path in target_files}
    holders: list[dict[str, Any]] = []
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        if not fd_dir.exists():
            continue
        open_files = _open_target_files(fd_dir, target_strings)
        if not open_files:
            continue
        pid = int(pid_dir.name)
        command = _process_command(pid_dir)
        elapsed_seconds = _process_elapsed_seconds(pid_dir)
        holders.append(
            {
                "pid": pid,
                "command": command,
                "open_files": sorted(open_files),
                "current_process": pid == os.getpid(),
                "likely_writer": _is_likely_writer(command),
                "elapsed_seconds": elapsed_seconds,
                "elapsed": _format_elapsed(elapsed_seconds),
            }
        )
    return sorted(holders, key=lambda item: item["pid"]), "procfs"


def _open_target_files(fd_dir: Path, target_strings: set[str]) -> set[str]:
    open_files: set[str] = set()
    try:
        descriptors = list(fd_dir.iterdir())
    except OSError:
        return open_files
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        normalized = _normalize_open_target(target)
        if normalized in target_strings:
            open_files.add(normalized)
    return open_files


def _process_command(pid_dir: Path) -> str:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        raw = b""
    command = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    if command:
        return command
    try:
        return (pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unknown"


def _process_elapsed_seconds(pid_dir: Path) -> int | None:
    try:
        stat_text = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        fields_after_comm = stat_text.rsplit(") ", 1)[1].split()
        process_start_ticks = int(fields_after_comm[19])
        system_uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
    except (IndexError, OSError, ValueError):
        return None
    elapsed = system_uptime - (process_start_ticks / ticks_per_second)
    return max(0, int(elapsed))


def _format_elapsed(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _normalize_open_target(target: str) -> str:
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    try:
        return _normalize_path(Path(target))
    except OSError:
        return target


def _normalize_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _is_likely_writer(command: str) -> bool:
    lowered = command.lower()
    return any(marker in lowered for marker in LIKELY_WRITER_MARKERS)


def _lock_status(
    holders: list[dict[str, Any]],
    writer_holders: list[dict[str, Any]],
    scan_method: str,
) -> str:
    if writer_holders:
        return "BUSY_WRITER"
    if holders:
        return "OPEN_READERS"
    if scan_method == "procfs_unavailable":
        return "UNKNOWN"
    return "CLEAR"


def _next_action(
    holders: list[dict[str, Any]],
    writer_holders: list[dict[str, Any]],
    scan_method: str,
) -> str:
    if writer_holders:
        return "Wait for the listed writer job to finish before starting another write command."
    if holders:
        return "Read-only holders are visible; avoid overlapping long write jobs."
    if scan_method == "procfs_unavailable":
        return "Process-level lock scanning is unavailable on this platform."
    return "No process appears to have the SQLite files open."


def _recommended_next_command(writer: dict[str, Any] | None) -> str:
    if writer is None:
        return "kalshi-bot db-locks"

    command = str(writer.get("command") or "").lower()
    if "market-legs-parse" in command:
        return "kalshi-bot link-remediate"
    if "link-remediate" in command:
        return "kalshi-bot derive-sports-schedule --build-features"
    if "derive-sports-schedule" in command:
        return "kalshi-bot market-coverage-doctor --output-dir reports/market_coverage"
    if "crypto-history-warmup" in command:
        return "kalshi-bot forecast --model crypto_v2"
    if "forecast" in command and "crypto_v2" in command:
        return "kalshi-bot active-crypto-router --output-dir reports/phase3at"
    if "find-opportunities" in command:
        return (
            "kalshi-bot learning-diagnostics --scan-limit 500 --suggest-thresholds "
            "--output reports/learning_diagnostics.md"
        )
    if "learning-once" in command:
        return "kalshi-bot paper-summary"
    if "settlement-watch" in command or "sync-settlements" in command:
        return (
            "kalshi-bot paper-settlement-doctor "
            "--output-dir reports/paper_settlement_reconciliation"
        )
    if "tonight-run" in command or "overnight-run" in command:
        return "kalshi-bot tonight-report --output reports/tonight_report.md"
    return "kalshi-bot db-locks"


def _monitor_next_action(
    *,
    current_writer: dict[str, Any] | None,
    safe_to_start_write: bool,
    recommended_command: str,
) -> str:
    if current_writer is not None:
        return (
            f"Wait for writer pid {current_writer['pid']} to finish, then run: "
            f"{recommended_command}"
        )
    if safe_to_start_write:
        return "No writer is active; safe to start the next write job after reviewing db-locks."
    return "A writer may be active but was not identified; wait and rerun db-writer-monitor."


def _same_pid(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _pid_exists(pid: object) -> bool:
    try:
        pid_int = int(str(pid))
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    return Path(f"/proc/{pid_int}").exists()
