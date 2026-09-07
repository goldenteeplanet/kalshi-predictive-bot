"""Select tests conservatively from a deterministic file-to-test dependency map."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

SCHEMA = "phase4ff.selector-input.v1"


def _hash(v: Any) -> str:
    if isinstance(v, dict):
        v = {k: x for k, x in v.items() if k != "artifact_hash"}
    return canonical_hash(v)


def _path(x: Any, code: str) -> str:
    if (
        not isinstance(x, str)
        or not x
        or x.startswith(("/", "\\"))
        or Path(x).is_absolute()
        or Path(x).drive
        or ".." in Path(x).parts
        or "\\" in x
    ):
        raise ValueError(code)
    return x


def build_report(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or set(p) != {
        "schema",
        "cache_hash",
        "changed_files",
        "dependency_map",
        "all_tests",
        "artifact_hash",
    }:
        raise ValueError("PHASE4FF_FIELDS_INVALID")
    if p["schema"] != SCHEMA or p["artifact_hash"] != _hash(p):
        raise ValueError("PHASE4FF_HASH_INVALID")
    changed = p["changed_files"]
    all_tests = p["all_tests"]
    mapping = p["dependency_map"]
    if not isinstance(changed, list) or len(changed) != len(set(changed)):
        raise ValueError("PHASE4FF_CHANGED_INVALID")
    if not isinstance(all_tests, list) or not all_tests or len(all_tests) != len(set(all_tests)):
        raise ValueError("PHASE4FF_TESTS_INVALID")
    changed = sorted(_path(x, "PHASE4FF_CHANGED_PATH_INVALID") for x in changed)
    all_tests = sorted(_path(x, "PHASE4FF_TEST_PATH_INVALID") for x in all_tests)
    if any(not x.startswith("tests/") for x in all_tests):
        raise ValueError("PHASE4FF_TEST_PATH_INVALID")
    if not isinstance(mapping, dict):
        raise ValueError("PHASE4FF_MAP_INVALID")
    normalized = {}
    unknown = []
    selected = set()
    for source, tests in mapping.items():
        source = _path(source, "PHASE4FF_MAP_PATH_INVALID")
        if (
            not isinstance(tests, list)
            or not tests
            or len(tests) != len(set(tests))
            or any(t not in all_tests for t in tests)
        ):
            raise ValueError("PHASE4FF_MAP_TEST_INVALID")
        normalized[source] = sorted(tests)
    for source in changed:
        if source not in normalized:
            unknown.append(source)
        else:
            selected.update(normalized[source])
    mode = (
        "FULL"
        if unknown
        or any(
            x.startswith(("pyproject.toml", "requirements", ".github/", "scripts/"))
            for x in changed
        )
        else "FOCUSED"
    )
    if mode == "FULL":
        selected = set(all_tests)
    r = {
        "schema": "phase4ff.selector-report.v1",
        "phase": "4FF",
        "cache_hash": p["cache_hash"],
        "input_hash": p["artifact_hash"],
        "changed_files": changed,
        "unknown_files": unknown,
        "selection_mode": mode,
        "selected_tests": sorted(selected),
        "all_tests": all_tests,
        "pre_merge_full_suite_required": True,
        "coverage_weakened": False,
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
