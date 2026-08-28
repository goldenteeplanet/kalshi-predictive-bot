from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

from scripts.local.phase4la_commit_ancestry import verify_commit_binding


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _proof(paths: list[str]) -> dict:
    payload = {
        "schema": "phase4kz.commit-payload-proof.v1",
        "verdict": "PASS",
        "owned_paths": sorted(paths),
        "staged_paths": sorted(paths),
        "records": [],
        "errors": [],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["proof_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _fixture(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "phase")
    _git(repo, "config", "user.email", "phase4la@example.invalid")
    _git(repo, "config", "user.name", "Phase 4LA")
    parent = _commit(repo, "base.txt", "base\n", "base")
    commit = _commit(repo, "phase.txt", "phase\n", "phase")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    return repo, parent, commit, tree, "phase"


def _verify(fixture, **overrides):
    repo, parent, commit, tree, branch = fixture
    values = {
        "commit": commit,
        "expected_parent": parent,
        "expected_tree": tree,
        "expected_branch": branch,
        "owned_paths": ["phase.txt"],
        "staged_proof": _proof(["phase.txt"]),
    }
    values.update(overrides)
    return verify_commit_binding(repo, **values)


def test_valid_binding_passes(tmp_path: Path) -> None:
    result = _verify(_fixture(tmp_path))
    assert result["verdict"] == "PASS"
    assert result["committed_paths"] == ["phase.txt"]


def test_parent_and_tree_mismatch_refuse(tmp_path: Path) -> None:
    result = _verify(_fixture(tmp_path), expected_parent="0" * 40, expected_tree="1" * 40)
    assert "PARENT_MISMATCH" in result["errors"]
    assert "TREE_MISMATCH" in result["errors"]


def test_committed_path_mismatch_refuses(tmp_path: Path) -> None:
    result = _verify(
        _fixture(tmp_path), owned_paths=["other.txt"], staged_proof=_proof(["other.txt"])
    )
    assert "UNDECLARED_COMMITTED_PATH:phase.txt" in result["errors"]
    assert "OWNED_PATH_NOT_COMMITTED:other.txt" in result["errors"]


def test_detached_head_refuses(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _git(tmp_path, "checkout", "--detach", "-q")
    assert "DETACHED_OR_UNEXPECTED_BRANCH" in _verify(fixture)["errors"]


def test_tampered_staged_proof_refuses(tmp_path: Path) -> None:
    proof = _proof(["phase.txt"])
    proof["verdict"] = "REFUSE"
    result = _verify(_fixture(tmp_path), staged_proof=proof)
    assert "STAGED_PROOF_NOT_PASSING" in result["errors"]
    assert "STAGED_PROOF_HASH_MISMATCH" in result["errors"]


def test_missing_commit_refuses(tmp_path: Path) -> None:
    result = _verify(_fixture(tmp_path), commit="f" * 40)
    assert "COMMIT_NOT_FOUND" in result["errors"]


def test_proof_path_mismatch_refuses(tmp_path: Path) -> None:
    proof = copy.deepcopy(_proof(["other.txt"]))
    result = _verify(_fixture(tmp_path), staged_proof=proof)
    assert "STAGED_PROOF_PATH_MISMATCH" in result["errors"]
