"""Independently recompute evidence-chain and Git bindings."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "phase4ld.independent-chain-audit.v1"
RECEIPT_SCHEMA = "phase4lb.commit-evidence-receipt.v1"
LEDGER_SCHEMA = "phase4lb.commit-evidence-ledger.v1"
CHECKPOINT_SCHEMA = "phase4lc.evidence-ledger-checkpoint.v1"
PHASE = re.compile(r"^4([A-Z])([A-Z])$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _hash(payload: dict[str, Any], field: str) -> str:
    body = {key: value for key, value in payload.items() if key != field}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True)


def _phase_number(value: object) -> int | None:
    match = PHASE.fullmatch(str(value))
    if not match:
        return None
    return (ord(match.group(1)) - 65) * 26 + ord(match.group(2)) - 65


def _safe_paths(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if value != sorted(set(value)):
        return False
    for item in value:
        if not isinstance(item, str) or not item or "\\" in item:
            return False
        path = PurePosixPath(item)
        if path.is_absolute() or "." in path.parts or ".." in path.parts:
            return False
    return True


def independent_audit(
    repo: Path, receipts: object, claimed_ledger: object, checkpoint: object
) -> dict[str, object]:
    root = repo.resolve()
    errors: list[str] = []
    rows = receipts if isinstance(receipts, list) else []
    if not isinstance(receipts, list) or not rows:
        errors.append("EMPTY_OR_INVALID_RECEIPTS")
    previous: dict[str, Any] | None = None
    seen_phases: set[str] = set()
    seen_commits: set[str] = set()
    for index, row in enumerate(rows):
        label = f"RECEIPT_{index}"
        if not isinstance(row, dict):
            errors.append(f"{label}:NOT_AN_OBJECT")
            continue
        if row.get("schema") != RECEIPT_SCHEMA:
            errors.append(f"{label}:BAD_SCHEMA")
        if row.get("receipt_sha256") != _hash(row, "receipt_sha256"):
            errors.append(f"{label}:HASH_MISMATCH")
        phase = str(row.get("phase_id", ""))
        phase_number = _phase_number(phase)
        if phase_number is None or phase in seen_phases:
            errors.append(f"{label}:BAD_OR_DUPLICATE_PHASE")
        seen_phases.add(phase)
        commit = str(row.get("commit", ""))
        if not HEX40.fullmatch(commit) or commit in seen_commits:
            errors.append(f"{label}:BAD_OR_DUPLICATE_COMMIT")
        seen_commits.add(commit)
        if not _safe_paths(row.get("owned_paths")):
            errors.append(f"{label}:UNSAFE_PATHS")
        if row.get("safety_verdict") != "PASS":
            errors.append(f"{label}:SAFETY_NOT_PASSING")
        tests = row.get("test_summary")
        if not isinstance(tests, dict) or tests.get("failed") != 0 or tests.get("passed", 0) < 1:
            errors.append(f"{label}:TESTS_NOT_PASSING")

        exists = _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
        if exists.returncode:
            errors.append(f"{label}:GIT_COMMIT_NOT_FOUND")
        else:
            parent_text = _git(root, "show", "-s", "--format=%P", commit).stdout.decode().strip()
            parents = parent_text.split() if parent_text else []
            if len(parents) != 1 or parents[0] != row.get("parent"):
                errors.append(f"{label}:GIT_PARENT_MISMATCH")
            tree = _git(root, "rev-parse", f"{commit}^{{tree}}").stdout.decode().strip()
            if tree != row.get("tree"):
                errors.append(f"{label}:GIT_TREE_MISMATCH")
            raw = _git(
                root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit
            ).stdout
            paths = sorted(
                field.decode("utf-8", errors="surrogateescape")
                for field in raw.split(b"\0")
                if field
            )
            if paths != row.get("owned_paths"):
                errors.append(f"{label}:GIT_PATH_MISMATCH")
        if previous is None:
            if row.get("previous_receipt_sha256") is not None:
                errors.append(f"{label}:UNEXPECTED_PREVIOUS_HASH")
        else:
            if row.get("previous_receipt_sha256") != previous.get("receipt_sha256"):
                errors.append(f"{label}:PREVIOUS_HASH_MISMATCH")
            if row.get("parent") != previous.get("commit"):
                errors.append(f"{label}:PARENT_CHAIN_MISMATCH")
            prior_number = _phase_number(previous.get("phase_id"))
            if prior_number is None or phase_number != prior_number + 1:
                errors.append(f"{label}:NON_CONTIGUOUS_PHASE")
        previous = row

    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    last = rows[-1] if rows and isinstance(rows[-1], dict) else {}
    recomputed: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "receipt_count": len(rows),
        "first_phase": first.get("phase_id"),
        "last_phase": last.get("phase_id"),
        "tip_receipt_sha256": last.get("receipt_sha256"),
        "errors": [],
        "safety": {
            "read_only_validation": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    recomputed["ledger_sha256"] = _hash(recomputed, "ledger_sha256")
    if not isinstance(claimed_ledger, dict) or claimed_ledger != recomputed:
        errors.append("CLAIMED_LEDGER_DISAGREES")
    if not isinstance(checkpoint, dict):
        errors.append("CHECKPOINT_NOT_AN_OBJECT")
        checkpoint = {}
    if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
        errors.append("CHECKPOINT_BAD_SCHEMA")
    if checkpoint.get("checkpoint_sha256") != _hash(checkpoint, "checkpoint_sha256"):
        errors.append("CHECKPOINT_HASH_MISMATCH")
    for field in (
        "ledger_sha256",
        "receipt_count",
        "first_phase",
        "last_phase",
        "tip_receipt_sha256",
    ):
        if checkpoint.get(field) != recomputed.get(field):
            errors.append(f"CHECKPOINT_{field.upper()}_MISMATCH")

    errors = sorted(set(errors))
    result: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "recomputed_ledger_sha256": recomputed["ledger_sha256"],
        "checked_receipts": len(rows),
        "checked_git_commits": len(seen_commits),
        "errors": errors,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    result["audit_sha256"] = _hash(result, "audit_sha256")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--claimed-ledger", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    values = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (args.receipts, args.claimed_ledger, args.checkpoint)
    ]
    result = independent_audit(args.repo, *values)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
