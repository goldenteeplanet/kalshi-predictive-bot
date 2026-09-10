"""Freeze committed model code before acquisition; not a model release certificate."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def freeze_code(repo: Path, output: Path, paths: tuple[str, ...]) -> dict[str, Any]:
    repo = repo.resolve(strict=True)

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={repo.as_posix()}", *args],
            cwd=repo, text=True, timeout=10,
        ).strip()

    if not paths or len(set(paths)) != len(paths):
        raise ValueError("EXPLICIT_UNIQUE_DEPENDENCIES_REQUIRED")
    head = git("rev-parse", "HEAD")
    committed = datetime.fromtimestamp(int(git("show", "-s", "--format=%ct", head)), UTC)
    frozen = datetime.now(UTC)
    if committed > frozen:
        raise ValueError("FUTURE_COMMIT_TIME")
    files = []
    for relative in paths:
        path = (repo / relative).resolve(strict=True)
        if not path.is_relative_to(repo) or path.is_symlink():
            raise ValueError("DEPENDENCY_OUTSIDE_REPO")
        if git("status", "--porcelain", "--", relative):
            raise ValueError("UNCOMMITTED_MODEL_DEPENDENCY")
        if git("hash-object", str(path)) != git("rev-parse", f"{head}:{relative}"):
            raise ValueError("MODEL_BLOB_MISMATCH")
        raw = path.read_bytes()
        files.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "raw": raw})
    output.mkdir(parents=True, exist_ok=False)
    for item in files:
        destination = output / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.pop("raw"))
    return {
        "source_commit": head, "commit_recorded_at": committed.isoformat(),
        "code_frozen_at": frozen.isoformat(), "files": files,
        "release_certified": False,
        "clock_authority": "LOCAL_CLOCK_AND_GIT_METADATA_NOT_EXTERNAL_ATTESTATION",
    }


def verify_unchanged(repo: Path, proof: dict[str, Any]) -> None:
    for item in proof["files"]:
        raw = (repo / item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("MODEL_CHANGED_DURING_CAPTURE")
