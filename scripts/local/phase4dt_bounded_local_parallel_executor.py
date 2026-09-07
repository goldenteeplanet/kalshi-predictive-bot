"""Execute fixed synthetic Decimal tasks with bounded local threads and stable output."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dt.executor-input.v1"
REPORT_SCHEMA = "phase4dt.executor-report.v1"
MAX_WORKERS = 8
MAX_TASKS = 1_000
MAX_VALUES_PER_TASK = 1_000
MAX_TOTAL_WORK_UNITS = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("PHASE4DT_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("PHASE4DT_DECIMAL_INVALID") from exc
    if not parsed.is_finite():
        raise ValueError("PHASE4DT_DECIMAL_INVALID")
    return parsed


def _render(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _execute(task: dict[str, Any]) -> dict[str, str]:
    values = [_decimal(value) for value in task["values"]]
    if task["operation"] == "SUM":
        result = sum(values, Decimal(0))
    elif task["operation"] == "SUM_OF_SQUARES":
        result = sum((value * value for value in values), Decimal(0))
    else:
        raise ValueError("PHASE4DT_OPERATION_INVALID")
    return {"task_id": task["task_id"], "result": _render(result)}


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {"schema", "synthetic_only", "worker_count", "tasks", "artifact_hash"}
    if set(payload) != required:
        raise ValueError("PHASE4DT_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DT_INPUT_SCHEMA_OR_HASH_INVALID")
    if payload["synthetic_only"] is not True:
        raise ValueError("PHASE4DT_SYNTHETIC_ONLY_REQUIRED")
    workers = payload["worker_count"]
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("PHASE4DT_WORKER_COUNT_INVALID")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise ValueError("PHASE4DT_TASK_COUNT_INVALID")
    fields = {"task_id", "operation", "values"}
    identifiers: set[str] = set()
    total_work = 0
    for task in tasks:
        if not isinstance(task, dict) or set(task) != fields:
            raise ValueError("PHASE4DT_TASK_FIELDS_INVALID")
        identifier = task["task_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DT_TASK_ID_INVALID")
        identifiers.add(identifier)
        if task["operation"] not in {"SUM", "SUM_OF_SQUARES"}:
            raise ValueError("PHASE4DT_OPERATION_INVALID")
        values = task["values"]
        if not isinstance(values, list) or not values or len(values) > MAX_VALUES_PER_TASK:
            raise ValueError("PHASE4DT_TASK_VALUES_INVALID")
        for value in values:
            _decimal(value)
        total_work += len(values)
    if total_work > MAX_TOTAL_WORK_UNITS:
        raise ValueError("PHASE4DT_TOTAL_WORK_LIMIT_EXCEEDED")
    sequential = sorted((_execute(task) for task in tasks), key=lambda row: row["task_id"])
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="phase4dt-synthetic") as pool:
        parallel = sorted(pool.map(_execute, tasks), key=lambda row: row["task_id"])
    if parallel != sequential:
        raise ValueError("PHASE4DT_PARALLEL_EQUIVALENCE_FAILED")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DT",
        "input_hash": payload["artifact_hash"],
        "worker_count": workers,
        "task_count": len(tasks),
        "total_work_units": total_work,
        "results": parallel,
        "parallel_results_hash": canonical_hash(parallel),
        "sequential_results_hash": canonical_hash(sequential),
        "deterministic_equivalence": True,
        "synthetic_only": True,
        "external_processes_launched": 0,
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
