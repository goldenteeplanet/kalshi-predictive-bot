from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.local.phase4kz_commit_payload_proof import verify_commit_payload


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "phase4kz@example.invalid")
    _git(path, "config", "user.name", "Phase 4KZ")
    return path


def _stage(repo: Path, path: str, content: str = "safe\n") -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", path)


def test_exact_staged_payload_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _stage(repo, "phase.py")
    result = verify_commit_payload(repo, ["phase.py"])
    assert result["verdict"] == "PASS"
    assert result["records"][0]["mode"] == "100644"


def test_unrelated_staged_path_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _stage(repo, "phase.py")
    _stage(repo, "unrelated.py")
    result = verify_commit_payload(repo, ["phase.py"])
    assert "UNDECLARED_STAGED_PATH:unrelated.py" in result["errors"]


def test_missing_owned_staged_path_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = verify_commit_payload(repo, ["phase.py"])
    assert "OWNED_PATH_NOT_STAGED:phase.py" in result["errors"]


def test_unstaged_divergence_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _stage(repo, "phase.py", "staged\n")
    (repo / "phase.py").write_text("changed later\n", encoding="utf-8")
    result = verify_commit_payload(repo, ["phase.py"])
    assert "INDEX_WORKTREE_DIVERGENCE:phase.py" in result["errors"]


def test_symlink_mode_refuses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "target.txt").write_text("target\n", encoding="utf-8")
    try:
        (repo / "phase-link").symlink_to("target.txt")
    except OSError:
        return
    _git(repo, "add", "phase-link")
    result = verify_commit_payload(repo, ["phase-link"])
    assert any(
        error.startswith("UNSAFE_INDEX_MODE:phase-link:120000") for error in result["errors"]
    )


def test_unsafe_and_duplicate_owned_paths_refuse(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = verify_commit_payload(repo, ["../escape", "phase.py", "phase.py"])
    assert "UNSAFE_OWNED_PATH" in result["errors"]
    assert "DUPLICATE_OWNED_PATH" in result["errors"]
