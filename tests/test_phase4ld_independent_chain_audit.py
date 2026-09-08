from __future__ import annotations

import copy
import subprocess
from pathlib import Path

from scripts.local.phase4lb_evidence_chain import create_receipt, validate_ledger
from scripts.local.phase4lc_ledger_checkpoint import create_checkpoint
from scripts.local.phase4ld_independent_chain_audit import independent_audit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgSign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, path: str) -> tuple[str, str, str]:
    parent = _git(repo, "rev-parse", "HEAD")
    target = repo / path
    target.write_text(path, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", path)
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    return parent, commit, tree


def _fixture(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "audit")
    _git(tmp_path, "config", "user.email", "phase4ld@example.invalid")
    _git(tmp_path, "config", "user.name", "Phase 4LD")
    (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-qm", "seed")
    parent, first_commit, first_tree = _commit(tmp_path, "kx.txt")
    first = create_receipt(
        phase_id="4KX",
        commit=first_commit,
        parent=parent,
        tree=first_tree,
        owned_paths=["kx.txt"],
        staged_proof_sha256="d" * 64,
        ancestry_binding_sha256="e" * 64,
        tests_passed=2,
        tests_skipped=0,
        completed_at="2026-08-28T20:00:00Z",
        previous_receipt_sha256=None,
    )
    parent, second_commit, second_tree = _commit(tmp_path, "ky.txt")
    second = create_receipt(
        phase_id="4KY",
        commit=second_commit,
        parent=parent,
        tree=second_tree,
        owned_paths=["ky.txt"],
        staged_proof_sha256="f" * 64,
        ancestry_binding_sha256="1" * 64,
        tests_passed=3,
        tests_skipped=0,
        completed_at="2026-08-28T20:01:00Z",
        previous_receipt_sha256=first["receipt_sha256"],
    )
    receipts = [first, second]
    ledger = validate_ledger(receipts)
    checkpoint = create_checkpoint(receipts, created_at="2026-08-28T20:02:00Z")
    return receipts, ledger, checkpoint


def test_independent_recomputation_passes(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    result = independent_audit(tmp_path, receipts, ledger, checkpoint)
    assert result["verdict"] == "PASS"
    assert result["checked_git_commits"] == 2


def test_claimed_implementation_disagreement_refuses(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    ledger["receipt_count"] = 99
    assert (
        "CLAIMED_LEDGER_DISAGREES"
        in independent_audit(tmp_path, receipts, ledger, checkpoint)["errors"]
    )


def test_git_tree_and_path_mismatch_refuse(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    receipts[1]["tree"] = "0" * 40
    receipts[1]["owned_paths"] = ["other.txt"]
    errors = independent_audit(tmp_path, receipts, ledger, checkpoint)["errors"]
    assert "RECEIPT_1:GIT_TREE_MISMATCH" in errors
    assert "RECEIPT_1:GIT_PATH_MISMATCH" in errors


def test_missing_git_commit_refuses(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    receipts[1]["commit"] = "9" * 40
    errors = independent_audit(tmp_path, receipts, ledger, checkpoint)["errors"]
    assert "RECEIPT_1:GIT_COMMIT_NOT_FOUND" in errors


def test_checkpoint_rollback_or_tampering_refuses(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    checkpoint["receipt_count"] = 1
    errors = independent_audit(tmp_path, receipts, ledger, checkpoint)["errors"]
    assert "CHECKPOINT_RECEIPT_COUNT_MISMATCH" in errors
    assert "CHECKPOINT_HASH_MISMATCH" in errors


def test_truncated_chain_refuses(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    result = independent_audit(tmp_path, receipts[:1], ledger, checkpoint)
    assert result["verdict"] == "REFUSE"
    assert "CLAIMED_LEDGER_DISAGREES" in result["errors"]


def test_altered_receipt_and_unsafe_path_refuse(tmp_path: Path) -> None:
    receipts, ledger, checkpoint = _fixture(tmp_path)
    altered = copy.deepcopy(receipts)
    altered[1]["owned_paths"] = ["../escape"]
    errors = independent_audit(tmp_path, altered, ledger, checkpoint)["errors"]
    assert "RECEIPT_1:HASH_MISMATCH" in errors
    assert "RECEIPT_1:UNSAFE_PATHS" in errors
