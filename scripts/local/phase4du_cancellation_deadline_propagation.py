"""Model local cancellation propagation and refuse partial or late publication."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4du.cancellation-input.v1"
REPORT_SCHEMA = "phase4du.cancellation-report.v1"
MAX_TASKS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DU_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DU_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DU_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DU_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DU_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evaluated_at", "tasks", "artifact_hash"}:
        raise ValueError("PHASE4DU_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DU_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _time(payload["evaluated_at"])
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise ValueError("PHASE4DU_TASK_COUNT_INVALID")
    fields = {
        "task_id",
        "dependencies",
        "started_at",
        "finished_at",
        "deadline",
        "complete",
        "result_hash",
    }
    by_id = {}
    parsed = {}
    for task in tasks:
        if not isinstance(task, dict) or set(task) != fields:
            raise ValueError("PHASE4DU_TASK_FIELDS_INVALID")
        identifier = task["task_id"]
        dependencies = task["dependencies"]
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise ValueError("PHASE4DU_TASK_ID_INVALID")
        if (
            not isinstance(dependencies, list)
            or len(set(dependencies)) != len(dependencies)
            or any(not isinstance(item, str) or not item for item in dependencies)
        ):
            raise ValueError("PHASE4DU_DEPENDENCIES_INVALID")
        if not isinstance(task["complete"], bool):
            raise ValueError("PHASE4DU_COMPLETION_INVALID")
        started, finished, deadline = (
            _time(task["started_at"]),
            _time(task["finished_at"]),
            _time(task["deadline"]),
        )
        if finished < started or evaluated_at < finished:
            raise ValueError("PHASE4DU_TASK_TIMELINE_INVALID")
        if task["complete"]:
            _digest(task["result_hash"])
        elif task["result_hash"] is not None:
            raise ValueError("PHASE4DU_PARTIAL_RESULT_HASH_INVALID")
        by_id[identifier] = task
        parsed[identifier] = (started, finished, deadline)
    if any(dependency not in by_id for task in tasks for dependency in task["dependencies"]):
        raise ValueError("PHASE4DU_DEPENDENCY_MISSING")
    statuses: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()

    def evaluate(identifier: str) -> dict[str, Any]:
        if identifier in visiting:
            raise ValueError("PHASE4DU_DEPENDENCY_CYCLE")
        if identifier in statuses:
            return statuses[identifier]
        visiting.add(identifier)
        task = by_id[identifier]
        dependencies = [evaluate(item) for item in task["dependencies"]]
        started, finished, deadline = parsed[identifier]
        reasons = []
        if any(not dependency["publishable"] for dependency in dependencies):
            reasons.append("DEPENDENCY_CANCELLED_OR_UNPUBLISHABLE")
        if started >= deadline:
            reasons.append("EXPIRED_BEFORE_OR_AT_START")
        elif finished > deadline:
            reasons.append("FINISHED_AFTER_DEADLINE")
        if not task["complete"]:
            reasons.append("INCOMPLETE_RESULT")
        status = {
            "task_id": identifier,
            "cancelled": bool(reasons),
            "publishable": not reasons,
            "reasons": reasons,
            "result_hash": task["result_hash"] if not reasons else None,
        }
        statuses[identifier] = status
        visiting.remove(identifier)
        return status

    for identifier in sorted(by_id):
        evaluate(identifier)
    decisions = [statuses[identifier] for identifier in sorted(statuses)]
    publishable = [
        {"task_id": row["task_id"], "result_hash": row["result_hash"]}
        for row in decisions
        if row["publishable"]
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DU",
        "input_hash": payload["artifact_hash"],
        "task_decisions": decisions,
        "publishable_results": publishable,
        "cancelled_task_ids": [row["task_id"] for row in decisions if row["cancelled"]],
        "partial_or_late_results_published": 0,
        "publication_hash": canonical_hash(publishable),
        "local_cancellation_only": True,
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
