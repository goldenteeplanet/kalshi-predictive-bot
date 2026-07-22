from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from kalshi_predictor.roadmap.deployment_preflight import build_deployment_preflight


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "before")
    rollback = _git(repo, "rev-parse", "HEAD")
    (repo / "file.txt").write_text("after\n", encoding="utf-8")
    _git(repo, "commit", "-am", "after")
    target = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", target)
    return repo, target, rollback


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    environment = tmp_path / "runtime.env"
    environment.write_text(
        "EXECUTION_ENABLED=false\nAUTOPILOT_ENABLED=false\n"
        "EXECUTION_KILL_SWITCH=true\nEXECUTION_GATEWAY_MODE=disabled\nSECRET=not-reported\n",
        encoding="utf-8",
    )
    backup = tmp_path / "backup.db"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
    return environment, backup


def test_builds_fail_closed_manifest_without_secret_values(tmp_path: Path) -> None:
    repo, target, rollback = _fixture_repo(tmp_path)
    environment, backup = _inputs(tmp_path)

    result = build_deployment_preflight(
        repo=repo,
        target_sha=target,
        rollback_sha=rollback,
        environment_file=environment,
        backup_database=backup,
    )

    assert result["target"]["sha"] == target
    assert result["backup"]["quick_check"] == "ok"
    assert result["backup"]["integrity_check"] == "ok"
    assert result["deployment_authorized"] is False
    assert result["live_execution_authorized"] is False
    assert "not-reported" not in str(result)
    assert len(result["manifest_sha256"]) == 64


def test_rejects_unsafe_environment(tmp_path: Path) -> None:
    repo, target, rollback = _fixture_repo(tmp_path)
    environment, backup = _inputs(tmp_path)
    environment.write_text("EXECUTION_ENABLED=true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not deployment-safe"):
        build_deployment_preflight(
            repo=repo,
            target_sha=target,
            rollback_sha=rollback,
            environment_file=environment,
            backup_database=backup,
        )


def test_rejects_target_that_is_not_origin_main(tmp_path: Path) -> None:
    repo, target, rollback = _fixture_repo(tmp_path)
    environment, backup = _inputs(tmp_path)
    _git(repo, "update-ref", "refs/remotes/origin/main", rollback)

    with pytest.raises(ValueError, match="origin/main"):
        build_deployment_preflight(
            repo=repo,
            target_sha=target,
            rollback_sha=rollback,
            environment_file=environment,
            backup_database=backup,
        )
