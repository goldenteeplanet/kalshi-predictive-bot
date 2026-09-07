"""Evaluate the fail-closed final gate for market-data Workstream II."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cz.final-gate-input.v1"
REPORT_SCHEMA = "phase4cz.final-gate-report.v1"
REQUIRED_PHASES = tuple(f"4C{suffix}" for suffix in "ABCDEFGHIJKLMNOPQRSTUVWXY")
PROOF_FIELDS = ("lineage", "deterministic_replay", "resource_bounds", "safety")


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {
            key: value
            for key, value in payload.items()
            if key not in {"artifact_hash", "evidence_hash"}
        }
    return canonical_hash(payload)


def _allowed_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    return (
        value == "docs/phase4-roadmap-progress.md"
        or value.startswith("docs/phase4c")
        or value.startswith("scripts/local/phase4c")
        or value.startswith("tests/test_phase4c")
    )


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
        "phase_evidence",
        "changed_paths",
        "cumulative_tests_passed",
        "expected_platform_skips",
        "ruff_passed",
        "artifact_hash",
    }
    if set(payload) != fields:
        raise ValueError("PHASE4CZ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CZ_INPUT_SCHEMA_OR_HASH_INVALID")
    rows = payload.get("phase_evidence")
    if not isinstance(rows, list) or len(rows) != len(REQUIRED_PHASES):
        raise ValueError("PHASE4CZ_EVIDENCE_COUNT_INVALID")
    required_row_fields = {"phase", "status", *PROOF_FIELDS, "evidence_hash"}
    by_phase = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != required_row_fields:
            raise ValueError("PHASE4CZ_EVIDENCE_FIELDS_INVALID")
        phase = row["phase"]
        if phase not in REQUIRED_PHASES or phase in by_phase:
            raise ValueError("PHASE4CZ_PHASE_SET_INVALID")
        if row["evidence_hash"] != _hash(row):
            raise ValueError("PHASE4CZ_EVIDENCE_HASH_INVALID")
        if row["status"] != "COMPLETE" or any(row[field] != "PASS" for field in PROOF_FIELDS):
            raise ValueError("PHASE4CZ_PHASE_PROOF_INVALID")
        by_phase[phase] = row
    if set(by_phase) != set(REQUIRED_PHASES):
        raise ValueError("PHASE4CZ_PHASE_SET_INVALID")

    changed_paths = payload.get("changed_paths")
    if (
        not isinstance(changed_paths, list)
        or not changed_paths
        or len(set(changed_paths)) != len(changed_paths)
    ):
        raise ValueError("PHASE4CZ_CHANGED_PATHS_INVALID")
    prohibited = sorted(path for path in changed_paths if not _allowed_path(path))
    tests = payload.get("cumulative_tests_passed")
    skips = payload.get("expected_platform_skips")
    if isinstance(tests, bool) or not isinstance(tests, int) or tests <= 0:
        raise ValueError("PHASE4CZ_TEST_COUNT_INVALID")
    if isinstance(skips, bool) or not isinstance(skips, int) or skips < 0:
        raise ValueError("PHASE4CZ_SKIP_COUNT_INVALID")
    if not isinstance(payload.get("ruff_passed"), bool):
        raise ValueError("PHASE4CZ_RUFF_STATUS_INVALID")
    passed = not prohibited and payload["ruff_passed"]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CZ",
        "input_hash": payload["artifact_hash"],
        "status": "WORKSTREAM_II_FINAL_GATE_PASS" if passed else "REFUSE",
        "passed": passed,
        "verified_phases": list(REQUIRED_PHASES),
        "cumulative_tests_passed": tests,
        "expected_platform_skips": skips,
        "ruff_passed": payload["ruff_passed"],
        "changed_paths": sorted(changed_paths),
        "prohibited_paths": prohibited,
        "production_collector_changes": 0 if not prohibited else len(prohibited),
        "advancement_authorized": passed,
        "trading_execution_authorized": False,
        "production_records_created": 0,
    }
    report["artifact_hash"] = _hash(report)
    return report


def publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.evidence.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
