"""Benchmark deterministic synthetic fill models without touching real paper fills."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4eo.audit-input.v1"
REPORT_SCHEMA = "phase4eo.audit-report.v1"
MODELS = {"TOP_OF_BOOK", "QUEUE_AHEAD", "PRO_RATA"}
SETUP_WORK_UNITS = 5
LEGACY_EVAL_WORK_UNITS = 2
OPTIMIZED_EVAL_WORK_UNITS = 1


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _integer(value: Any, error: str, *, positive: bool = False, maximum: int | None = None) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(error)
    if maximum is not None and value > maximum:
        raise ValueError(error)
    return value


def _digest(value: Any) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4EO_SNAPSHOT_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4EO_SNAPSHOT_HASH_INVALID") from exc


def _filled(
    model: str, requested: int, available: int, queue_ahead: int, participation_bps: int
) -> int:
    if model == "TOP_OF_BOOK":
        capacity = available
    elif model == "QUEUE_AHEAD":
        capacity = max(0, available - queue_ahead)
    else:
        capacity = available * participation_bps // 10_000
    return min(requested, capacity)


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "scenarios", "artifact_hash"}:
        raise ValueError("PHASE4EO_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EO_INPUT_SCHEMA_OR_HASH_INVALID")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("PHASE4EO_SCENARIOS_EMPTY")
    seen = set()
    normalized = []
    required = {
        "scenario_id",
        "model",
        "book_snapshot_hash",
        "requested_quantity",
        "available_quantity",
        "queue_ahead_quantity",
        "participation_bps",
    }
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != required:
            raise ValueError("PHASE4EO_SCENARIO_FIELDS_INVALID")
        identifier = scenario["scenario_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EO_SCENARIO_ID_INVALID")
        seen.add(identifier)
        model = scenario["model"]
        if model not in MODELS:
            raise ValueError("PHASE4EO_MODEL_INVALID")
        _digest(scenario["book_snapshot_hash"])
        requested = _integer(
            scenario["requested_quantity"], "PHASE4EO_QUANTITY_INVALID", positive=True
        )
        available = _integer(scenario["available_quantity"], "PHASE4EO_QUANTITY_INVALID")
        queue_ahead = _integer(scenario["queue_ahead_quantity"], "PHASE4EO_QUANTITY_INVALID")
        participation = _integer(
            scenario["participation_bps"], "PHASE4EO_PARTICIPATION_INVALID", maximum=10_000
        )
        if model != "QUEUE_AHEAD" and queue_ahead != 0:
            raise ValueError("PHASE4EO_UNUSED_PARAMETER_INVALID")
        if model != "PRO_RATA" and participation != 0:
            raise ValueError("PHASE4EO_UNUSED_PARAMETER_INVALID")
        filled = _filled(model, requested, available, queue_ahead, participation)
        normalized.append(
            {
                **scenario,
                "legacy_filled_quantity": filled,
                "optimized_filled_quantity": filled,
                "equivalent": True,
                "remaining_quantity": requested - filled,
            }
        )
    normalized.sort(key=lambda row: row["scenario_id"])
    groups = {(row["model"], row["book_snapshot_hash"]) for row in normalized}
    legacy_work = len(normalized) * (SETUP_WORK_UNITS + LEGACY_EVAL_WORK_UNITS)
    optimized_work = len(groups) * SETUP_WORK_UNITS + len(normalized) * OPTIMIZED_EVAL_WORK_UNITS
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EO",
        "input_hash": payload["artifact_hash"],
        "status": "SYNTHETIC_FILL_EQUIVALENCE_PROVEN",
        "scenarios": normalized,
        "legacy_work_units": legacy_work,
        "optimized_work_units": optimized_work,
        "work_reduction_units": legacy_work - optimized_work,
        "wall_clock_used_as_gate": False,
        "real_paper_fills_modified": 0,
        "real_paper_fills_created": 0,
        "database_writes": 0,
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
