"""Fail-closed verifier for phase-owned worktree change boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

from scripts.local.phase4kx_workspace_provenance import SCHEMA as MANIFEST_SCHEMA

SCHEMA = "phase4ky.change-boundary-verdict.v1"
ALLOWLIST_SCHEMA = "phase4ky.phase-owned-paths.v1"


def _canonical_hash(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and "." not in path.parts


def _validate_manifest(payload: object, label: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [f"{label}:NOT_AN_OBJECT"]
    if payload.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"{label}:BAD_SCHEMA")
    if payload.get("manifest_sha256") != _canonical_hash(payload):
        errors.append(f"{label}:HASH_MISMATCH")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return [*errors, f"{label}:ENTRIES_NOT_A_LIST"]
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not _safe_path(entry.get("path")):
            errors.append(f"{label}:UNSAFE_OR_MALFORMED_PATH")
            continue
        paths.append(entry["path"])
    if len(paths) != len(set(paths)):
        errors.append(f"{label}:DUPLICATE_PATH")
    if payload.get("entry_count") != len(entries):
        errors.append(f"{label}:COUNT_MISMATCH")
    return errors


def verify_boundary(baseline: object, current: object, allowlist: object) -> dict[str, object]:
    errors = _validate_manifest(baseline, "BASELINE")
    errors.extend(_validate_manifest(current, "CURRENT"))
    if not isinstance(allowlist, dict) or allowlist.get("schema") != ALLOWLIST_SCHEMA:
        errors.append("ALLOWLIST:BAD_SCHEMA")
        owned: list[object] = []
    else:
        owned = allowlist.get("owned_paths", [])
        if not isinstance(owned, list):
            errors.append("ALLOWLIST:PATHS_NOT_A_LIST")
            owned = []
    if any(not _safe_path(path) for path in owned):
        errors.append("ALLOWLIST:UNSAFE_PATH")
    owned_paths = {str(path) for path in owned if _safe_path(path)}
    if len(owned_paths) != len(owned):
        errors.append("ALLOWLIST:DUPLICATE_PATH")

    baseline_entries = baseline.get("entries", []) if isinstance(baseline, dict) else []
    current_entries = current.get("entries", []) if isinstance(current, dict) else []
    baseline_rows = {
        row["path"]: row
        for row in baseline_entries
        if isinstance(row, dict) and _safe_path(row.get("path"))
    }
    current_rows = {
        row["path"]: row
        for row in current_entries
        if isinstance(row, dict) and _safe_path(row.get("path"))
    }
    for path in sorted(set(baseline_rows) | set(current_rows)):
        if path in owned_paths:
            continue
        if baseline_rows.get(path) != current_rows.get(path):
            errors.append(f"UNRELATED_CHANGE:{path}")
        row = current_rows.get(path)
        if row and str(row.get("status", " "))[0] not in {" ", "?"}:
            errors.append(f"UNRELATED_STAGED:{path}")
    for path in sorted(owned_paths):
        if path not in current_rows:
            errors.append(f"OWNED_PATH_MISSING:{path}")

    errors = sorted(set(errors))
    return {
        "schema": SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "owned_paths": sorted(owned_paths),
        "errors": errors,
        "safety": {
            "read_only": True,
            "database_access": False,
            "service_control": False,
            "order_capability": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--allowlist", required=True)
    args = parser.parse_args()
    with open(args.baseline, encoding="utf-8") as stream:
        baseline = json.load(stream)
    with open(args.current, encoding="utf-8") as stream:
        current = json.load(stream)
    with open(args.allowlist, encoding="utf-8") as stream:
        allowlist = json.load(stream)
    result = verify_boundary(baseline, current, allowlist)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
