"""Deterministic receipts and chain validation for completed engineering phases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

RECEIPT_SCHEMA = "phase4lb.commit-evidence-receipt.v1"
LEDGER_SCHEMA = "phase4lb.commit-evidence-ledger.v1"
PHASE_PATTERN = re.compile(r"^4([A-Z])([A-Z])$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _digest(payload: dict[str, Any], hash_field: str) -> str:
    body = {key: value for key, value in payload.items() if key != hash_field}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _phase_index(phase_id: str) -> int | None:
    match = PHASE_PATTERN.fullmatch(phase_id)
    if not match:
        return None
    return (ord(match.group(1)) - ord("A")) * 26 + ord(match.group(2)) - ord("A")


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and "." not in path.parts and ".." not in path.parts


def create_receipt(
    *,
    phase_id: str,
    commit: str,
    parent: str,
    tree: str,
    owned_paths: list[str],
    staged_proof_sha256: str,
    ancestry_binding_sha256: str,
    tests_passed: int,
    tests_skipped: int,
    completed_at: str,
    previous_receipt_sha256: str | None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "phase_id": phase_id,
        "commit": commit,
        "parent": parent,
        "tree": tree,
        "owned_paths": sorted(owned_paths),
        "staged_proof_sha256": staged_proof_sha256,
        "ancestry_binding_sha256": ancestry_binding_sha256,
        "test_summary": {"passed": tests_passed, "failed": 0, "skipped": tests_skipped},
        "safety_verdict": "PASS",
        "completed_at": completed_at,
        "previous_receipt_sha256": previous_receipt_sha256,
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    return receipt


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def validate_ledger(receipts: object) -> dict[str, object]:
    errors: list[str] = []
    if not isinstance(receipts, list):
        receipts = []
        errors.append("LEDGER_NOT_A_LIST")
    seen_phases: set[str] = set()
    seen_commits: set[str] = set()
    previous: dict[str, Any] | None = None
    previous_time: datetime | None = None
    for offset, item in enumerate(receipts):
        label = f"RECEIPT_{offset}"
        if not isinstance(item, dict):
            errors.append(f"{label}:NOT_AN_OBJECT")
            continue
        if item.get("schema") != RECEIPT_SCHEMA:
            errors.append(f"{label}:BAD_SCHEMA")
        if item.get("receipt_sha256") != _digest(item, "receipt_sha256"):
            errors.append(f"{label}:HASH_MISMATCH")
        phase_id = str(item.get("phase_id", ""))
        phase_index = _phase_index(phase_id)
        if phase_index is None:
            errors.append(f"{label}:BAD_PHASE_ID")
        if phase_id in seen_phases:
            errors.append(f"{label}:DUPLICATE_PHASE_ID")
        seen_phases.add(phase_id)
        commit = str(item.get("commit", ""))
        if not HEX40.fullmatch(commit):
            errors.append(f"{label}:BAD_COMMIT")
        if commit in seen_commits:
            errors.append(f"{label}:DUPLICATE_COMMIT")
        seen_commits.add(commit)
        if not HEX40.fullmatch(str(item.get("parent", ""))):
            errors.append(f"{label}:BAD_PARENT")
        if not HEX40.fullmatch(str(item.get("tree", ""))):
            errors.append(f"{label}:BAD_TREE")
        for field in ("staged_proof_sha256", "ancestry_binding_sha256"):
            if not HEX64.fullmatch(str(item.get(field, ""))):
                errors.append(f"{label}:BAD_{field.upper()}")
        paths = item.get("owned_paths")
        if not isinstance(paths, list) or not paths:
            errors.append(f"{label}:EMPTY_OR_INVALID_PATHS")
        elif any(not _safe_path(path) for path in paths):
            errors.append(f"{label}:UNSAFE_PATH")
        elif paths != sorted(set(paths)):
            errors.append(f"{label}:PATHS_NOT_CANONICAL")
        tests = item.get("test_summary")
        if (
            not isinstance(tests, dict)
            or not isinstance(tests.get("passed"), int)
            or tests.get("passed", 0) < 1
            or tests.get("failed") != 0
            or not isinstance(tests.get("skipped"), int)
            or tests.get("skipped", -1) < 0
        ):
            errors.append(f"{label}:TESTS_NOT_PASSING")
        if item.get("safety_verdict") != "PASS":
            errors.append(f"{label}:SAFETY_NOT_PASSING")
        completed = _timestamp(item.get("completed_at"))
        if completed is None:
            errors.append(f"{label}:BAD_TIMESTAMP")
        elif previous_time is not None and completed <= previous_time:
            errors.append(f"{label}:NON_MONOTONIC_TIMESTAMP")
        if completed is not None:
            previous_time = completed
        if previous is None:
            if item.get("previous_receipt_sha256") is not None:
                errors.append(f"{label}:FIRST_RECEIPT_HAS_PREDECESSOR")
        else:
            if item.get("previous_receipt_sha256") != previous.get("receipt_sha256"):
                errors.append(f"{label}:PREVIOUS_HASH_MISMATCH")
            if item.get("parent") != previous.get("commit"):
                errors.append(f"{label}:PARENT_CHAIN_MISMATCH")
            prior_index = _phase_index(str(previous.get("phase_id", "")))
            if prior_index is None or phase_index is None or phase_index != prior_index + 1:
                errors.append(f"{label}:NON_CONTIGUOUS_PHASE")
        previous = item

    errors = sorted(set(errors))
    payload: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "receipt_count": len(receipts),
        "first_phase": receipts[0].get("phase_id")
        if receipts and isinstance(receipts[0], dict)
        else None,
        "last_phase": receipts[-1].get("phase_id")
        if receipts and isinstance(receipts[-1], dict)
        else None,
        "tip_receipt_sha256": receipts[-1].get("receipt_sha256")
        if receipts and isinstance(receipts[-1], dict)
        else None,
        "errors": errors,
        "safety": {
            "read_only_validation": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    payload["ledger_sha256"] = _digest(payload, "ledger_sha256")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=str)
    args = parser.parse_args()
    with open(args.ledger, encoding="utf-8") as stream:
        receipts = json.load(stream)
    result = validate_ledger(receipts)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
