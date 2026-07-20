from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kalshi_predictor.r5_recovery7 import estimate_progress
from kalshi_predictor.ui.backup_verification import normalize_backup_verification

SHA256_PATTERN = re.compile(r"^([0-9a-fA-F]{64})\s+(.+)$")


def adapt_captured_verification(
    sources: Mapping[str, Any], *, previous_snapshot: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    diagnostics: list[str] = []
    process = _process(dict(sources.get("process") or {}), diagnostics)
    database = dict(sources.get("database") or {})
    io_status = str(sources.get("proc_io_status") or "available")
    io_restricted = io_status == "permission_restricted"
    io = (
        {}
        if io_restricted
        else _proc_io(str(sources.get("proc_io") or ""), diagnostics)
    )
    integrity_text = str(sources.get("integrity_output") or "").strip()
    sha_text = str(sources.get("sha256_output") or "").strip()
    execution_text = str(sources.get("execution") or "").strip().lower()
    execution_enabled = execution_text not in {"false", "execution_enabled=false"}
    if execution_enabled:
        diagnostics.append("EXECUTION_STATE_NOT_DISABLED")
    database_bytes = _integer(database.get("bytes"), diagnostics, "DATABASE_SIZE_INVALID")
    previous = dict((previous_snapshot or {}).get("backup_verification") or {})
    previous_read = previous.get("raw_read_bytes")
    sample_age = sources.get("previous_sample_age_seconds")
    progress = (
        {
            "read_bytes": None,
            "progress_percent_lower_bound": None,
            "estimated_remaining_seconds": None,
            "io_advanced": None,
            "stale": False,
        }
        if io_restricted
        else estimate_progress(
            database_bytes=database_bytes or 0,
            read_bytes_start=int(database.get("read_bytes_start") or 0),
            read_bytes_current=io.get("read_bytes", 0),
            elapsed_seconds=process.get("elapsed_seconds", 0),
            previous_read_bytes=int(previous_read) if previous_read is not None else None,
            previous_sample_age_seconds=float(sample_age) if sample_age is not None else None,
        )
    )
    integrity_status = "OK" if integrity_text == "ok" else "FAILED" if integrity_text else "PENDING"
    sha_match = SHA256_PATTERN.match(sha_text)
    sha_status = "VERIFIED" if sha_match else "FAILED" if sha_text else "PENDING"
    running = process["running"]
    command = process["command"]
    if running and "integrity_check" in command:
        stage = "integrity_check"
    elif running and "sha256sum" in command:
        stage = "sha256"
    elif integrity_status == "OK" and sha_status == "VERIFIED":
        stage = "certified"
    elif integrity_status == "OK":
        stage = "sha256"
    else:
        stage = "integrity_check"
    if execution_enabled or diagnostics:
        state = "BLOCKED"
    elif stage == "certified":
        state = "PASSED"
    elif running:
        state = "RUNNING"
    elif integrity_status == "FAILED" or sha_status == "FAILED":
        state = "FAILED"
    else:
        state = "WAITING"
    raw = {
        "state": state,
        "stage": stage,
        "pid": process["pid"],
        "elapsed_seconds": process["elapsed_seconds"],
        "database_bytes": database_bytes,
        "read_bytes": progress["read_bytes"],
        "progress_percent_lower_bound": progress["progress_percent_lower_bound"],
        "estimated_remaining_seconds": progress["estimated_remaining_seconds"],
        "io_advanced": progress["io_advanced"],
        "stale": progress["stale"],
        "integrity_status": integrity_status,
        "sha256_status": sha_status,
        "path": database.get("path"),
    }
    normalized, normalize_diagnostics = normalize_backup_verification(raw)
    diagnostics.extend(normalize_diagnostics)
    normalized["raw_read_bytes"] = io.get("read_bytes")
    normalized["io_visibility"] = "PERMISSION_RESTRICTED" if io_restricted else "AVAILABLE"
    normalized["progress_available"] = not io_restricted
    normalized["sha256"] = sha_match.group(1).lower() if sha_match else None
    normalized["collector_diagnostics"] = sorted(set(diagnostics))
    normalized["execution_enabled"] = execution_enabled
    normalized["source_mode"] = "CAPTURED_READ_ONLY"
    return normalized


def publish_verification(snapshot: Mapping[str, Any], destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    payload = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
    return {
        "published": True,
        "destination": str(destination),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "temporary_absent": not temporary.exists(),
        "database_writes": 0,
        "cloud_access": False,
    }


def collect_local_verification(
    *,
    database_path: Path,
    integrity_output_path: Path,
    sha256_output_path: Path,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    process = _discover_process(proc_root, database_path)
    proc_io = ""
    proc_io_status = "available"
    if process["pid"] is not None:
        try:
            proc_io = (proc_root / str(process["pid"]) / "io").read_text(encoding="utf-8")
        except PermissionError:
            proc_io_status = "permission_restricted"
        except OSError:
            proc_io = ""
    sources = {
        "process": process,
        "database": {
            "path": str(database_path),
            "bytes": database_path.stat().st_size if database_path.exists() else None,
            "read_bytes_start": 0,
        },
        "proc_io": proc_io,
        "proc_io_status": proc_io_status,
        "integrity_output": _read_optional(integrity_output_path),
        "sha256_output": _read_optional(sha256_output_path),
        "execution": "EXECUTION_ENABLED=false",
    }
    return adapt_captured_verification(sources)


def _discover_process(proc_root: Path, database_path: Path) -> dict[str, Any]:
    target = str(database_path)
    matches: list[tuple[int, Path, str]] = []
    for candidate in sorted(proc_root.iterdir(), key=lambda path: path.name):
        if not candidate.name.isdigit():
            continue
        try:
            command = (
                (candidate / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
            )
        except OSError:
            continue
        if target not in command or not any(
            marker in command for marker in ("integrity_check", "sha256sum")
        ):
            continue
        priority = 0 if command.startswith(("sqlite3 ", "sha256sum ")) else 1
        matches.append((priority, candidate, command))
    if matches:
        _, candidate, command = min(matches, key=lambda item: (item[0], int(item[1].name)))
        try:
            stat_fields = (candidate / "stat").read_text(encoding="utf-8").split()
            uptime = float((proc_root / "uptime").read_text(encoding="utf-8").split()[0])
            ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            elapsed = max(0.0, uptime - float(stat_fields[21]) / ticks)
        except (OSError, ValueError, IndexError):
            elapsed = 0.0
        return {
            "pid": int(candidate.name),
            "running": True,
            "elapsed_seconds": elapsed,
            "command": command,
        }
    return {"pid": None, "running": False, "elapsed_seconds": 0, "command": ""}


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _process(raw: dict[str, Any], diagnostics: list[str]) -> dict[str, Any]:
    pid = _integer(raw.get("pid"), diagnostics, "PROCESS_PID_INVALID")
    elapsed = _number(raw.get("elapsed_seconds"), diagnostics, "PROCESS_ELAPSED_INVALID")
    return {
        "pid": pid,
        "elapsed_seconds": elapsed or 0.0,
        "running": raw.get("running") is True and pid is not None,
        "command": str(raw.get("command") or ""),
    }


def _proc_io(text: str, diagnostics: list[str]) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        try:
            values[key.strip()] = int(value.strip())
        except ValueError:
            diagnostics.append(f"PROC_IO_INVALID:{key.strip()}")
    if "read_bytes" not in values:
        diagnostics.append("PROC_IO_READ_BYTES_MISSING")
    return values


def _integer(value: Any, diagnostics: list[str], code: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        diagnostics.append(code)
        return None


def _number(value: Any, diagnostics: list[str], code: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        diagnostics.append(code)
        return None
