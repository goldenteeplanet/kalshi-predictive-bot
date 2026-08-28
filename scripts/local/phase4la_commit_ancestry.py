"""Bind a phase commit to its parent, branch, tree, paths, and staged proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "phase4la.commit-ancestry-binding.v1"
STAGED_PROOF_SCHEMA = "phase4kz.commit-payload-proof.v1"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and "." not in path.parts and ".." not in path.parts


def _proof_hash(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "proof_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def verify_commit_binding(
    repo: Path,
    *,
    commit: str,
    expected_parent: str,
    expected_tree: str,
    expected_branch: str,
    owned_paths: list[object],
    staged_proof: object,
) -> dict[str, object]:
    root = repo.resolve()
    errors: list[str] = []
    safe_owned = [str(path) for path in owned_paths if _safe_path(path)]
    if len(safe_owned) != len(owned_paths):
        errors.append("UNSAFE_OWNED_PATH")
    if len(safe_owned) != len(set(safe_owned)):
        errors.append("DUPLICATE_OWNED_PATH")
    expected_paths = set(safe_owned)

    commit_check = _git(root, "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    if commit_check.returncode:
        errors.append("COMMIT_NOT_FOUND")
        resolved_commit = ""
        parents: list[str] = []
        tree = ""
        committed_paths: set[str] = set()
    else:
        resolved_commit = _git(root, "rev-parse", commit).stdout.decode().strip()
        parent_line = (
            _git(root, "show", "-s", "--format=%P", resolved_commit).stdout.decode().strip()
        )
        parents = parent_line.split() if parent_line else []
        tree = _git(root, "rev-parse", f"{resolved_commit}^{{tree}}").stdout.decode().strip()
        raw_paths = _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            resolved_commit,
        ).stdout
        committed_paths = {
            field.decode("utf-8", errors="surrogateescape")
            for field in raw_paths.split(b"\0")
            if field
        }
        if len(parents) != 1:
            errors.append("COMMIT_MUST_HAVE_EXACTLY_ONE_PARENT")
        elif parents[0] != expected_parent:
            errors.append("PARENT_MISMATCH")
        if tree != expected_tree:
            errors.append("TREE_MISMATCH")
        for path in sorted(committed_paths - expected_paths):
            errors.append(f"UNDECLARED_COMMITTED_PATH:{path}")
        for path in sorted(expected_paths - committed_paths):
            errors.append(f"OWNED_PATH_NOT_COMMITTED:{path}")

    head_ref = _git(root, "symbolic-ref", "--quiet", "HEAD", check=False)
    expected_ref = f"refs/heads/{expected_branch}"
    if head_ref.returncode or head_ref.stdout.decode().strip() != expected_ref:
        errors.append("DETACHED_OR_UNEXPECTED_BRANCH")
    head = _git(root, "rev-parse", "HEAD", check=False)
    if head.returncode or head.stdout.decode().strip() != resolved_commit:
        errors.append("COMMIT_IS_NOT_BRANCH_HEAD")

    if not isinstance(staged_proof, dict):
        errors.append("STAGED_PROOF_NOT_AN_OBJECT")
        proof_hash = ""
    else:
        proof_hash = str(staged_proof.get("proof_sha256", ""))
        if staged_proof.get("schema") != STAGED_PROOF_SCHEMA:
            errors.append("STAGED_PROOF_BAD_SCHEMA")
        if staged_proof.get("verdict") != "PASS":
            errors.append("STAGED_PROOF_NOT_PASSING")
        if staged_proof.get("proof_sha256") != _proof_hash(staged_proof):
            errors.append("STAGED_PROOF_HASH_MISMATCH")
        if staged_proof.get("owned_paths") != sorted(expected_paths):
            errors.append("STAGED_PROOF_PATH_MISMATCH")

    errors = sorted(set(errors))
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "commit": resolved_commit,
        "parents": parents,
        "tree": tree,
        "branch_ref": expected_ref,
        "committed_paths": sorted(committed_paths),
        "owned_paths": sorted(expected_paths),
        "staged_proof_sha256": proof_hash,
        "errors": errors,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["binding_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expected-parent", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--owned-path", action="append", default=[])
    parser.add_argument("--staged-proof", type=Path, required=True)
    args = parser.parse_args()
    proof = json.loads(args.staged_proof.read_text(encoding="utf-8"))
    payload = verify_commit_binding(
        args.repo,
        commit=args.commit,
        expected_parent=args.expected_parent,
        expected_tree=args.expected_tree,
        expected_branch=args.expected_branch,
        owned_paths=args.owned_path,
        staged_proof=proof,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
