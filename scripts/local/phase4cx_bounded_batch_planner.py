"""Plan deterministic bounded fixture batches without executing data collection."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cx.batch-input.v1"
REPORT_SCHEMA = "phase4cx.batch-plan.v1"
LIMIT_FIELDS = ("max_batch_items", "max_batch_bytes", "max_batch_memory_bytes", "rate_units")
MAX_FIXTURES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _positive(value: Any, error: str, *, zero_allowed: bool = False) -> int:
    lower = 0 if zero_allowed else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < lower:
        raise ValueError(error)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "limits", "fixtures", "artifact_hash"}:
        raise ValueError("PHASE4CX_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CX_INPUT_SCHEMA_OR_HASH_INVALID")
    limits = payload.get("limits")
    if not isinstance(limits, dict) or set(limits) != set(LIMIT_FIELDS):
        raise ValueError("PHASE4CX_LIMIT_FIELDS_INVALID")
    for field in LIMIT_FIELDS:
        _positive(limits[field], f"PHASE4CX_{field.upper()}_INVALID")
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures or len(fixtures) > MAX_FIXTURES:
        raise ValueError("PHASE4CX_FIXTURE_COUNT_INVALID")

    identifiers: set[str] = set()
    normalized = []
    required = {
        "fixture_id",
        "estimated_bytes",
        "memory_bytes",
        "estimated_ms",
        "rate_units",
        "deadline_ms",
    }
    for fixture in fixtures:
        if not isinstance(fixture, dict) or set(fixture) != required:
            raise ValueError("PHASE4CX_FIXTURE_FIELDS_INVALID")
        identifier = fixture["fixture_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CX_FIXTURE_ID_INVALID")
        identifiers.add(identifier)
        for field in required - {"fixture_id"}:
            _positive(fixture[field], f"PHASE4CX_{field.upper()}_INVALID", zero_allowed=True)
        normalized.append(fixture)
    ordered = sorted(normalized, key=lambda row: (row["deadline_ms"], row["fixture_id"]))

    batches = []
    refused = []
    current: list[dict[str, Any]] = []
    current_bytes = current_memory = current_rate = current_duration = 0
    elapsed = total_rate = 0

    def finalize() -> None:
        nonlocal current, current_bytes, current_memory, current_rate, current_duration, elapsed
        if not current:
            return
        elapsed += current_duration
        batches.append(
            {
                "batch_index": len(batches) + 1,
                "fixture_ids": [row["fixture_id"] for row in current],
                "item_count": len(current),
                "bytes": current_bytes,
                "memory_bytes": current_memory,
                "rate_units": current_rate,
                "duration_ms": current_duration,
                "completion_ms": elapsed,
            }
        )
        current = []
        current_bytes = current_memory = current_rate = current_duration = 0

    for fixture in ordered:
        individual_reasons = []
        if fixture["estimated_bytes"] > limits["max_batch_bytes"]:
            individual_reasons.append("ITEM_BYTES_EXCEED_BATCH_BOUND")
        if fixture["memory_bytes"] > limits["max_batch_memory_bytes"]:
            individual_reasons.append("ITEM_MEMORY_EXCEED_BATCH_BOUND")
        if fixture["rate_units"] > limits["rate_units"]:
            individual_reasons.append("ITEM_RATE_EXCEED_WINDOW_BOUND")
        if fixture["estimated_ms"] > fixture["deadline_ms"]:
            individual_reasons.append("ITEM_CANNOT_MEET_DEADLINE")
        if individual_reasons:
            refused.append({"fixture_id": fixture["fixture_id"], "reasons": individual_reasons})
            continue
        if total_rate + fixture["rate_units"] > limits["rate_units"]:
            refused.append(
                {"fixture_id": fixture["fixture_id"], "reasons": ["RATE_WINDOW_EXHAUSTED"]}
            )
            continue

        proposed_duration = max(current_duration, fixture["estimated_ms"])
        fits_current = (
            len(current) + 1 <= limits["max_batch_items"]
            and current_bytes + fixture["estimated_bytes"] <= limits["max_batch_bytes"]
            and current_memory + fixture["memory_bytes"] <= limits["max_batch_memory_bytes"]
            and elapsed + proposed_duration <= fixture["deadline_ms"]
        )
        if current and not fits_current:
            finalize()
        if elapsed + fixture["estimated_ms"] > fixture["deadline_ms"]:
            refused.append({"fixture_id": fixture["fixture_id"], "reasons": ["DEADLINE_MISSED"]})
            continue
        current.append(fixture)
        current_bytes += fixture["estimated_bytes"]
        current_memory += fixture["memory_bytes"]
        current_rate += fixture["rate_units"]
        current_duration = max(current_duration, fixture["estimated_ms"])
        total_rate += fixture["rate_units"]
    finalize()

    planned = sum(batch["item_count"] for batch in batches)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CX",
        "input_hash": payload["artifact_hash"],
        "status": "PLAN_COMPLETE" if planned + len(refused) == len(fixtures) else "REFUSE",
        "batches": batches,
        "refused": refused,
        "planned_fixture_count": planned,
        "refused_fixture_count": len(refused),
        "total_rate_units": total_rate,
        "planner_only": True,
        "execution_authorized": False,
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
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.fixtures.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
