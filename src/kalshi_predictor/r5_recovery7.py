from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

STAGES = ("backup_complete", "quick_check", "sha256", "integrity_check", "certified")


def sqlite_check(path: Path, pragma: str) -> dict[str, Any]:
    if pragma not in {"quick_check", "integrity_check"}:
        raise ValueError("unsupported SQLite verification pragma")
    started = time.perf_counter()
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
        finally:
            connection.close()
        error = None
    except sqlite3.DatabaseError as exc:
        rows = []
        error = str(exc)
    return {
        "pragma": pragma,
        "status": "ok" if rows == ["ok"] and error is None else "failed",
        "rows": rows,
        "error": error,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> dict[str, Any]:
    started = time.perf_counter()
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            bytes_read += len(chunk)
    return {
        "status": "ok",
        "sha256": digest.hexdigest(),
        "bytes_read": bytes_read,
        "runtime_seconds": round(time.perf_counter() - started, 6),
    }


def estimate_progress(
    *,
    database_bytes: int,
    read_bytes_start: int,
    read_bytes_current: int,
    elapsed_seconds: float,
    previous_read_bytes: int | None = None,
    previous_sample_age_seconds: float | None = None,
    stale_after_seconds: float = 300.0,
) -> dict[str, Any]:
    consumed = max(0, read_bytes_current - read_bytes_start)
    fraction = min(1.0, consumed / database_bytes) if database_bytes > 0 else 0.0
    rate = consumed / elapsed_seconds if elapsed_seconds > 0 else 0.0
    remaining = max(0, database_bytes - consumed)
    eta = remaining / rate if rate > 0 else None
    io_advanced = previous_read_bytes is None or read_bytes_current > previous_read_bytes
    stale = (
        previous_read_bytes is not None
        and not io_advanced
        and previous_sample_age_seconds is not None
        and previous_sample_age_seconds >= stale_after_seconds
    )
    return {
        "database_bytes": database_bytes,
        "read_bytes": consumed,
        "progress_percent_lower_bound": round(fraction * 100.0, 3),
        "average_read_bytes_per_second": round(rate, 3),
        "estimated_remaining_seconds": round(eta, 3) if eta is not None else None,
        "io_advanced": io_advanced,
        "stale": stale,
        "safe_to_interrupt": False,
    }


def staged_verification(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "database_path": str(path),
        "database_size_bytes": path.stat().st_size,
        "required_final_gate": "PRAGMA integrity_check returns exactly ok",
        "stages": [],
    }
    metadata["stages"].append({"stage": "backup_complete", "status": "passed"})
    quick = sqlite_check(path, "quick_check")
    metadata["stages"].append({"stage": "quick_check", **quick})
    if quick["status"] != "ok":
        return _finish(metadata, "failed", "quick_check")
    sha = file_sha256(path)
    metadata["stages"].append({"stage": "sha256", **sha})
    integrity = sqlite_check(path, "integrity_check")
    metadata["stages"].append({"stage": "integrity_check", **integrity})
    if integrity["status"] != "ok":
        return _finish(metadata, "failed", "integrity_check")
    metadata["stages"].append({"stage": "certified", "status": "passed"})
    return _finish(metadata, "certified", None)


def benchmark_fixture(path: Path) -> dict[str, Any]:
    quick = sqlite_check(path, "quick_check")
    full = sqlite_check(path, "integrity_check")
    return {
        "database_size_bytes": path.stat().st_size,
        "quick_check": quick,
        "integrity_check": full,
        "full_to_quick_runtime_ratio": (
            round(full["runtime_seconds"] / quick["runtime_seconds"], 3)
            if quick["runtime_seconds"] > 0
            else None
        ),
        "full_integrity_gate_preserved": True,
    }


def _finish(metadata: dict[str, Any], status: str, failed_stage: str | None) -> dict[str, Any]:
    metadata["status"] = status
    metadata["failed_stage"] = failed_stage
    metadata["execution_enabled"] = False
    metadata["metadata_sha256"] = hashlib.sha256(
        _canonical(metadata).encode("utf-8")
    ).hexdigest()
    return metadata


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(report), encoding="utf-8")
    return path


def _canonical(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
