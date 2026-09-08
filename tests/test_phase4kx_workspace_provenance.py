from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.local.phase4kx_workspace_provenance import SCHEMA, build_manifest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_manifest_is_deterministic_and_read_only(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "phase4kx@example.invalid")
    _git(tmp_path, "config", "user.name", "Phase 4KX")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "fixture")
    tracked.write_text("after\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")

    before = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout
    first = build_manifest(tmp_path)
    second = build_manifest(tmp_path)
    after = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    ).stdout

    assert first == second
    assert before == after
    assert first["schema"] == SCHEMA
    assert first["entry_count"] == 2
    assert [row["path"] for row in first["entries"]] == ["new.txt", "tracked.txt"]
    assert first["safety"]["order_capability"] is False


def test_clean_repository_has_empty_manifest(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    payload = build_manifest(tmp_path)
    assert payload["entry_count"] == 0
    assert payload["entries"] == []
