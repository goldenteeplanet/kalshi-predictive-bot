from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_SAFETY_VALUES = {
    "AUTOPILOT_ENABLED": "false",
    "EXECUTION_ENABLED": "false",
    "EXECUTION_GATEWAY_MODE": "disabled",
    "EXECUTION_KILL_SWITCH": "true",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _validate_commit(repo: Path, sha: str, *, label: str) -> None:
    if not SHA_PATTERN.fullmatch(sha):
        raise ValueError(f"{label} must be an exact lowercase 40-character SHA")
    _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")


def _database_checks(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if quick_check.lower() != "ok" or integrity_check.lower() != "ok":
        raise ValueError("backup database integrity checks did not return ok")
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "quick_check": quick_check,
        "integrity_check": integrity_check,
    }


def build_deployment_preflight(
    *,
    repo: Path,
    target_sha: str,
    rollback_sha: str,
    environment_file: Path,
    backup_database: Path,
) -> dict[str, Any]:
    repo = repo.resolve()
    environment_file = environment_file.resolve()
    backup_database = backup_database.resolve()
    _validate_commit(repo, target_sha, label="target_sha")
    _validate_commit(repo, rollback_sha, label="rollback_sha")
    if _git(repo, "status", "--porcelain"):
        raise ValueError("repository worktree is not clean")
    remote_main = _git(repo, "rev-parse", "origin/main")
    if target_sha != remote_main:
        raise ValueError("target_sha does not exactly match origin/main")
    if not environment_file.is_file():
        raise ValueError("environment file is missing")
    if not backup_database.is_file():
        raise ValueError("backup database is missing")

    environment = _read_environment(environment_file)
    safety = {
        key: {
            "expected": expected,
            "actual": environment.get(key),
            "passed": environment.get(key, "").lower() == expected,
        }
        for key, expected in REQUIRED_SAFETY_VALUES.items()
    }
    if not all(item["passed"] for item in safety.values()):
        raise ValueError("environment safety configuration is not deployment-safe")

    changed_files = _git(repo, "diff", "--name-only", f"{rollback_sha}..{target_sha}").splitlines()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "READ_ONLY_DEPLOYMENT_PREPARATION",
        "deployment_authorized": False,
        "paper_order_creation_authorized": False,
        "live_execution_authorized": False,
        "target": {
            "sha": target_sha,
            "origin_main_sha": remote_main,
            "tree_sha": _git(repo, "rev-parse", f"{target_sha}^{{tree}}"),
        },
        "rollback": {
            "sha": rollback_sha,
            "command_preview": f"git checkout --detach {rollback_sha}",
            "rehearsed": False,
        },
        "configuration": {
            "path": str(environment_file),
            "sha256": _sha256(environment_file),
            "safety": safety,
        },
        "backup": _database_checks(backup_database),
        "changed_files": changed_files,
        "read_only_smoke_commands": [
            ".venv/bin/kalshi-bot --help",
            ".venv/bin/kalshi-bot db-writer-monitor --json",
            ".venv/bin/kalshi-bot phase3bc-r5-status --output-dir reports/phase3bc_r5",
            f"scripts/cloud/verify-paper-deployment.sh {target_sha}",
        ],
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
