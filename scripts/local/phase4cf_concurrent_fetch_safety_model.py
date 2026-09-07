"""Model bounded concurrent fetch behavior without executing connected I/O."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cf.concurrent-fetch-simulation.v1"
REPORT_SCHEMA = "phase4cf.concurrent-fetch-safety-model.v1"
OUTCOMES = ("SUCCESS", "THROTTLED", "TIMEOUT", "MALFORMED", "CANCELLED")
MAX_CONCURRENCY = 32
MAX_RETRIES = 8


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def build_model(simulation: dict[str, Any]) -> dict[str, Any]:
    if simulation.get("schema") != INPUT_SCHEMA or simulation.get("artifact_hash") != _hash(
        simulation
    ):
        raise ValueError("PHASE4CF_INPUT_SCHEMA_OR_HASH_INVALID")
    concurrency = simulation.get("max_concurrency")
    retries = simulation.get("retry_limit")
    operations = simulation.get("operations")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= MAX_CONCURRENCY
    ):
        raise ValueError("PHASE4CF_CONCURRENCY_INVALID")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise ValueError("PHASE4CF_RETRY_LIMIT_INVALID")
    if not isinstance(operations, list) or not operations:
        raise ValueError("PHASE4CF_OPERATIONS_MISSING")
    identities: set[str] = set()
    ordinals: set[int] = set()
    results = []
    for operation in operations:
        fields = {"request_id", "ordinal", "outcomes", "result_hash"}
        if not isinstance(operation, dict) or set(operation) != fields:
            raise ValueError("PHASE4CF_OPERATION_FIELDS_INVALID")
        request_id, ordinal = operation["request_id"], operation["ordinal"]
        if not isinstance(request_id, str) or not request_id or request_id in identities:
            raise ValueError("PHASE4CF_REQUEST_ID_INVALID")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal in ordinals
        ):
            raise ValueError("PHASE4CF_ORDINAL_INVALID")
        identities.add(request_id)
        ordinals.add(ordinal)
        outcomes = operation["outcomes"]
        if (
            not isinstance(outcomes, list)
            or not outcomes
            or any(value not in OUTCOMES for value in outcomes)
        ):
            raise ValueError("PHASE4CF_OUTCOMES_INVALID")
        allowed_attempts = min(len(outcomes), retries + 1)
        consumed = outcomes[:allowed_attempts]
        status = "RETRY_EXHAUSTED"
        attempts_used = len(consumed)
        for attempt_number, outcome in enumerate(consumed, start=1):
            if outcome == "SUCCESS":
                status = "SUCCESS"
                attempts_used = attempt_number
                break
            if outcome in ("MALFORMED", "CANCELLED"):
                status = outcome
                attempts_used = attempt_number
                break
        result_hash = operation["result_hash"]
        if status == "SUCCESS":
            if not isinstance(result_hash, str) or len(result_hash) != 64:
                raise ValueError("PHASE4CF_SUCCESS_RESULT_HASH_INVALID")
        elif result_hash is not None:
            raise ValueError("PHASE4CF_NON_SUCCESS_RESULT_HASH_INVALID")
        results.append(
            {
                "request_id": request_id,
                "ordinal": ordinal,
                "assigned_slot": ordinal % concurrency,
                "attempt_count": attempts_used,
                "status": status,
                "throttle_count": consumed.count("THROTTLED"),
                "result_hash": result_hash,
            }
        )
    ordered = sorted(results, key=lambda row: row["ordinal"])
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CF",
        "input_hash": simulation["artifact_hash"],
        "max_concurrency": concurrency,
        "retry_limit": retries,
        "results": ordered,
        "merged_success_hashes": [
            row["result_hash"] for row in ordered if row["status"] == "SUCCESS"
        ],
        "failure_request_ids": [row["request_id"] for row in ordered if row["status"] != "SUCCESS"],
        "maximum_slot_used": max(row["assigned_slot"] for row in ordered),
        "deterministic_merge": True,
        "simulation_only": True,
        "production_records_created": 0,
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
    parser.add_argument("--simulation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_model(json.loads(args.simulation.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
