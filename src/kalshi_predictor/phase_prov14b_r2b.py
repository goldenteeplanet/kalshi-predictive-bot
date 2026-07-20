"""Normalize bounded runtime exports into PROV-14B-R2A evidence inputs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 5 * 1024 * 1024
EXPECTED_SERVICES = (
    "bounded_service",
    "bounded_timer",
    "legacy_watcher",
    "legacy_watcher_enabled",
    "other_writer",
)


def capture_runtime_evidence(
    *,
    backup_path: Path,
    writer_monitor_path: Path,
    locks_path: Path,
    services_path: Path,
    execution_path: Path,
    cycle_path: Path,
    attribution_path: Path,
    rollback_root: Path,
    rollback_paths: list[str],
    captured_at: datetime,
) -> dict[str, Any]:
    """Read bounded local exports only and produce exact R2A-compatible evidence."""
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    sources = {
        "backup": Path(backup_path),
        "writer_monitor": Path(writer_monitor_path),
        "locks": Path(locks_path),
        "services": Path(services_path),
        "execution": Path(execution_path),
        "cycle": Path(cycle_path),
        "attribution": Path(attribution_path),
    }
    source_rows = [_source(name, path) for name, path in sorted(sources.items())]
    backup = _object(backup_path, "backup")
    cycle = _object(cycle_path, "cycle")
    attribution = _object(attribution_path, "attribution")
    services = _services(_object(services_path, "services"))
    writer = _bounded_text(writer_monitor_path)
    locks = _bounded_text(locks_path)
    execution = _execution(_bounded_text(execution_path))
    rollback = _rollback_manifest(rollback_root, rollback_paths)
    safety = {
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "safe_to_start_write": _writer_clear(writer),
        "locks_clear": _locks_clear(locks),
        "execution_enabled": execution,
        "services": services,
    }
    diagnostics = []
    if safety["safe_to_start_write"] is not True:
        diagnostics.append("WRITER_CLEARANCE_NOT_PROVEN")
    if safety["locks_clear"] is not True:
        diagnostics.append("LOCK_CLEARANCE_NOT_PROVEN")
    if safety["execution_enabled"] is not False:
        diagnostics.append("EXECUTION_DISABLED_NOT_PROVEN")
    if set(services) != set(EXPECTED_SERVICES):
        diagnostics.append("SERVICE_EVIDENCE_INCOMPLETE")
    if not rollback["files"]:
        diagnostics.append("ROLLBACK_MANIFEST_EMPTY")
    report: dict[str, Any] = {
        "phase": "PROV-14B-R2B",
        "mode": "LOCAL_READ_ONLY_RUNTIME_EXPORT_CAPTURE",
        "status": "PASSED" if not diagnostics else "FAILED",
        "captured_at": captured_at.astimezone(UTC).isoformat(),
        "sources": source_rows,
        "diagnostics": diagnostics,
        "r2a_inputs": {
            "backup": backup,
            "rollback": rollback,
            "safety": safety,
            "cycle": cycle,
            "attribution": attribution,
        },
        "guardrails": {
            "cloud_access": False,
            "database_opened": False,
            "database_writes": 0,
            "service_changes": 0,
            "execution_changes": 0,
            "threshold_changes": 0,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical(report).encode()).hexdigest()
    return report


def write_capture(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(_canonical(report), encoding="utf-8")
    temporary.replace(output)
    return output


def _source(name: str, path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{name} source is missing: {path}")
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError(f"{name} source exceeds {MAX_SOURCE_BYTES} bytes")
    return {"name": name, "path": str(path), "size_bytes": size, "sha256": _sha(path)}


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_bounded_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} source is malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} source must contain a JSON object")
    return value


def _bounded_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"source is missing: {path}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes: {path}")
    return path.read_text(encoding="utf-8")


def _execution(text: str) -> bool | None:
    meaningful = [line.strip() for line in text.splitlines() if line.strip()]
    if len(meaningful) != 1 or not meaningful[0].startswith("EXECUTION_ENABLED="):
        raise ValueError("execution export must contain only EXECUTION_ENABLED")
    value = meaningful[0].split("=", 1)[1].strip().lower()
    if value == "false":
        return False
    if value == "true":
        return True
    return None


def _services(value: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(value) - set(EXPECTED_SERVICES))
    if unknown:
        raise ValueError("services export contains unexpected fields: " + ",".join(unknown))
    return {name: value[name] for name in EXPECTED_SERVICES if name in value}


def _writer_clear(text: str) -> bool:
    return bool(
        re.search(r"^DB writer monitor:\s*CLEAR\s*$", text, re.MULTILINE)
        and re.search(r"^Safe to start another write job:\s*yes\s*$", text, re.MULTILINE)
        and re.search(r"^Current writer PID:\s*none\s*$", text, re.MULTILINE)
    )


def _locks_clear(text: str) -> bool:
    return bool(
        re.search(r"^Database lock diagnostics:\s*CLEAR\s*$", text, re.MULTILINE)
        and re.search(r"^Safe to start another write job:\s*yes\s*$", text, re.MULTILINE)
        and re.search(r"^Open DB holders:\s*none visible\s*$", text, re.MULTILINE)
    )


def _rollback_manifest(root: Path, relative_paths: list[str]) -> dict[str, Any]:
    resolved_root = root.resolve()
    files = []
    for relative in sorted(set(relative_paths)):
        candidate = (root / relative).resolve()
        if not _is_relative_to(candidate, resolved_root):
            raise ValueError(f"rollback path escapes root: {relative}")
        if not candidate.is_file():
            raise ValueError(f"rollback file is missing: {relative}")
        files.append({"path": relative, "sha256": _sha(candidate)})
    return {"files": files}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"
