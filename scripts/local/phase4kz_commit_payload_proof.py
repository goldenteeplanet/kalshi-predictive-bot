"""Prove that Git's staged payload contains exactly the declared phase files."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

SCHEMA = "phase4kz.commit-payload-proof.v1"
SAFE_MODES = {"100644", "100755"}


def _run(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    parsed = PurePosixPath(value)
    return not parsed.is_absolute() and "." not in parsed.parts and ".." not in parsed.parts


def _staged_paths(repo: Path) -> list[str]:
    raw = _run(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRD")
    return sorted(
        field.decode("utf-8", errors="surrogateescape") for field in raw.split(b"\0") if field
    )


def _index_record(repo: Path, path: str) -> tuple[str, str, str] | None:
    raw = _run(repo, "ls-files", "--stage", "-z", "--", path)
    rows = [field for field in raw.split(b"\0") if field]
    if len(rows) != 1:
        return None
    metadata, actual_path = rows[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="surrogateescape") != path:
        return None
    mode, object_name, stage = metadata.decode().split()
    return mode, object_name, stage


def verify_commit_payload(repo: Path, owned_paths: list[object]) -> dict[str, object]:
    root = repo.resolve()
    errors: list[str] = []
    if any(not _safe_path(path) for path in owned_paths):
        errors.append("UNSAFE_OWNED_PATH")
    safe_owned = [str(path) for path in owned_paths if _safe_path(path)]
    if len(safe_owned) != len(set(safe_owned)):
        errors.append("DUPLICATE_OWNED_PATH")
    expected = set(safe_owned)
    staged = set(_staged_paths(root))
    for path in sorted(staged - expected):
        errors.append(f"UNDECLARED_STAGED_PATH:{path}")
    for path in sorted(expected - staged):
        errors.append(f"OWNED_PATH_NOT_STAGED:{path}")

    records: list[dict[str, str]] = []
    for path in sorted(expected & staged):
        record = _index_record(root, path)
        if record is None:
            errors.append(f"AMBIGUOUS_OR_MISSING_INDEX_ENTRY:{path}")
            continue
        mode, index_hash, stage = record
        if mode not in SAFE_MODES:
            errors.append(f"UNSAFE_INDEX_MODE:{path}:{mode}")
        if stage != "0":
            errors.append(f"NONZERO_INDEX_STAGE:{path}:{stage}")
        worktree_path = root / path
        if not worktree_path.is_file() or worktree_path.is_symlink():
            errors.append(f"UNSAFE_OR_MISSING_WORKTREE_FILE:{path}")
            continue
        worktree_hash = _run(root, "hash-object", "--", path).decode().strip()
        if worktree_hash != index_hash:
            errors.append(f"INDEX_WORKTREE_DIVERGENCE:{path}")
        records.append({"path": path, "mode": mode, "index_hash": index_hash, "stage": stage})

    errors = sorted(set(errors))
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "repository": str(root),
        "owned_paths": sorted(expected),
        "staged_paths": sorted(staged),
        "records": records,
        "errors": errors,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["proof_sha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--owned-path", action="append", default=[])
    args = parser.parse_args()
    payload = verify_commit_payload(args.repo, args.owned_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
