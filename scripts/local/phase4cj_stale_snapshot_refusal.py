"""Refuse stale snapshot artifacts before expensive downstream evaluation."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4cj.snapshot-batch.v1"
REPORT_SCHEMA = "phase4cj.stale-refusal.v1"
MAX_AGE_MS = 86_400_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4CJ_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4CJ_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4CJ_TIMESTAMP_INVALID")
    return parsed


def _milliseconds(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "evaluated_at", "max_age_ms", "snapshots", "artifact_hash"}:
        raise ValueError("PHASE4CJ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4CJ_INPUT_SCHEMA_OR_HASH_INVALID")
    evaluated_at = _timestamp(payload.get("evaluated_at"))
    max_age = payload.get("max_age_ms")
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 0 <= max_age <= MAX_AGE_MS:
        raise ValueError("PHASE4CJ_MAX_AGE_INVALID")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("PHASE4CJ_SNAPSHOTS_MISSING")

    identifiers: set[str] = set()
    decisions = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "snapshot_id",
            "captured_at",
            "content_hash",
        }:
            raise ValueError("PHASE4CJ_SNAPSHOT_FIELDS_INVALID")
        identifier = snapshot["snapshot_id"]
        content_hash = snapshot["content_hash"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4CJ_SNAPSHOT_ID_INVALID")
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise ValueError("PHASE4CJ_CONTENT_HASH_INVALID")
        try:
            int(content_hash, 16)
        except ValueError as exc:
            raise ValueError("PHASE4CJ_CONTENT_HASH_INVALID") from exc
        identifiers.add(identifier)
        captured_at = _timestamp(snapshot["captured_at"])
        age = _milliseconds(evaluated_at, captured_at)
        if age < 0:
            decision, reason = "REFUSE", "FUTURE_SNAPSHOT"
        elif age > max_age:
            decision, reason = "REFUSE", "STALE_SNAPSHOT"
        else:
            decision, reason = "ACCEPT", "WITHIN_FRESHNESS_BOUND"
        decisions.append(
            {
                "snapshot_id": identifier,
                "content_hash": content_hash,
                "age_ms": age,
                "decision": decision,
                "reason": reason,
                "downstream_evaluation_required": decision == "ACCEPT",
            }
        )

    refused = sum(row["decision"] == "REFUSE" for row in decisions)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4CJ",
        "input_hash": payload["artifact_hash"],
        "max_age_ms": max_age,
        "decisions": decisions,
        "accepted_count": len(decisions) - refused,
        "early_refusal_count": refused,
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
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.batch.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
