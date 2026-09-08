"""Build deterministic dependency-aware CI shards with a mandatory full merge suite."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4fd.partition-input.v1"


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or set(p) != {
        "schema",
        "permission_hash",
        "shard_count",
        "tests",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FD_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FD_HASH_INVALID")
    n = p["shard_count"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > 32:
        raise ValueError("PHASE4FD_SHARD_COUNT_INVALID")
    tests = p["tests"]
    if not isinstance(tests, list) or not tests:
        raise ValueError("PHASE4FD_TESTS_INVALID")
    seen = set()
    rows = []
    for t in tests:
        if not isinstance(t, dict) or set(t) != {"node_id", "path", "duration_ms", "dependencies"}:
            raise ValueError("PHASE4FD_TEST_FIELDS_INVALID")
        if t["node_id"] in seen or not isinstance(t["node_id"], str) or not t["node_id"]:
            raise ValueError("PHASE4FD_DUPLICATE_NODE")
        seen.add(t["node_id"])
        if (
            not isinstance(t["path"], str)
            or not t["path"].startswith("tests/")
            or ".." in Path(t["path"]).parts
        ):
            raise ValueError("PHASE4FD_PATH_INVALID")
        if (
            isinstance(t["duration_ms"], bool)
            or not isinstance(t["duration_ms"], int)
            or t["duration_ms"] < 0
        ):
            raise ValueError("PHASE4FD_DURATION_INVALID")
        if not isinstance(t["dependencies"], list) or len(t["dependencies"]) != len(
            set(t["dependencies"])
        ):
            raise ValueError("PHASE4FD_DEPENDENCIES_INVALID")
        rows.append({**t, "dependencies": sorted(t["dependencies"])})
    if any(d not in seen for t in rows for d in t["dependencies"]):
        raise ValueError("PHASE4FD_UNKNOWN_DEPENDENCY")
    # Longest-processing-time assignment is stable and balances historical duration.
    shards = [{"id": i, "duration_ms": 0, "tests": []} for i in range(n)]
    for t in sorted(rows, key=lambda x: (-x["duration_ms"], x["node_id"])):
        target = min(shards, key=lambda x: (x["duration_ms"], x["id"]))
        target["tests"].append(t["node_id"])
        target["duration_ms"] += t["duration_ms"]
    for s in shards:
        s["tests"].sort()
    ordered = sorted(rows, key=lambda x: x["node_id"])
    r = {
        "schema": "phase4fd.partition-report.v1",
        "phase": "4FD",
        "permission_hash": p["permission_hash"],
        "input_hash": p["artifact_hash"],
        "shards": shards,
        "full_merge_suite": [t["node_id"] for t in ordered],
        "full_merge_suite_required": True,
        "test_count": len(rows),
        "tests_omitted": False,
        "ci_config_changed": False,
        "production_database_mutated": False,
        "services_controlled": False,
        "execution_authorized": False,
    }
    r["artifact_hash"] = _hash(r)
    return r


def publish(path: Path, p: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, n = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(n)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(p, h, sort_keys=True, separators=(",", ":"))
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--input", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    x = a.parse_args()
    publish(x.output, build_report(json.loads(x.input.read_text())))


if __name__ == "__main__":
    main()
