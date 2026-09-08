"""Model independent offline computation stages and deterministic join behavior."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ds.parallelism-input.v1"
REPORT_SCHEMA = "phase4ds.parallelism-report.v1"
MAX_TASKS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DS_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DS_HASH_INVALID") from exc
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "tasks", "artifact_hash"}:
        raise ValueError("PHASE4DS_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DS_INPUT_SCHEMA_OR_HASH_INVALID")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise ValueError("PHASE4DS_TASK_COUNT_INVALID")
    fields = {"task_id", "dependencies", "result_hash", "work_units"}
    by_id = {}
    for task in tasks:
        if not isinstance(task, dict) or set(task) != fields:
            raise ValueError("PHASE4DS_TASK_FIELDS_INVALID")
        identifier = task["task_id"]
        dependencies = task["dependencies"]
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise ValueError("PHASE4DS_TASK_ID_INVALID")
        if (
            not isinstance(dependencies, list)
            or len(set(dependencies)) != len(dependencies)
            or any(not isinstance(item, str) or not item for item in dependencies)
        ):
            raise ValueError("PHASE4DS_DEPENDENCIES_INVALID")
        _digest(task["result_hash"])
        if (
            not isinstance(task["work_units"], int)
            or isinstance(task["work_units"], bool)
            or task["work_units"] < 1
        ):
            raise ValueError("PHASE4DS_WORK_UNITS_INVALID")
        by_id[identifier] = task
    if any(dependency not in by_id for task in tasks for dependency in task["dependencies"]):
        raise ValueError("PHASE4DS_DEPENDENCY_MISSING")
    levels: dict[str, int] = {}
    visiting: set[str] = set()

    def level(identifier: str) -> int:
        if identifier in visiting:
            raise ValueError("PHASE4DS_DEPENDENCY_CYCLE")
        if identifier in levels:
            return levels[identifier]
        visiting.add(identifier)
        dependencies = by_id[identifier]["dependencies"]
        result = 0 if not dependencies else 1 + max(level(item) for item in dependencies)
        visiting.remove(identifier)
        levels[identifier] = result
        return result

    for identifier in sorted(by_id):
        level(identifier)
    stages = []
    for stage_index in range(max(levels.values()) + 1):
        members = sorted(identifier for identifier, value in levels.items() if value == stage_index)
        stages.append(
            {
                "stage_index": stage_index,
                "task_ids": members,
                "parallel_width": len(members),
                "stage_work_units": sum(by_id[item]["work_units"] for item in members),
            }
        )
    canonical_join = [
        {"task_id": identifier, "result_hash": by_id[identifier]["result_hash"]}
        for identifier in sorted(by_id)
    ]
    sequential_join = sorted(
        ({"task_id": task["task_id"], "result_hash": task["result_hash"]} for task in tasks),
        key=lambda row: row["task_id"],
    )
    if canonical_join != sequential_join:
        raise ValueError("PHASE4DS_JOIN_EQUIVALENCE_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DS",
        "input_hash": payload["artifact_hash"],
        "stages": stages,
        "maximum_parallel_width": max(stage["parallel_width"] for stage in stages),
        "canonical_join": canonical_join,
        "parallel_join_hash": canonical_hash(canonical_join),
        "sequential_join_hash": canonical_hash(sequential_join),
        "deterministic_join_equivalence": True,
        "parallel_execution_enabled": False,
        "task_records_created": 0,
        "execution_authorized": False,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
