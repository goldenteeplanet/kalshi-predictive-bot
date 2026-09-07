"""Schedule candidate risk evaluations deterministically by deadline and freshness."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ei.schedule-input.v1"
REPORT_SCHEMA = "phase4ei.schedule-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4EI_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4EI_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("PHASE4EI_TIMESTAMP_INVALID")
    return parsed


def _milliseconds(delta: Any) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _nonnegative_int(value: Any, error: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(error)
    return value


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"schema", "as_of", "candidates", "artifact_hash"}:
        raise ValueError("PHASE4EI_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EI_INPUT_SCHEMA_OR_HASH_INVALID")
    as_of = _timestamp(payload["as_of"])
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("PHASE4EI_CANDIDATES_EMPTY")
    seen = set()
    scheduled = []
    refused = []
    for row in candidates:
        fields = {
            "candidate_id",
            "deadline",
            "evidence_observed_at",
            "freshness_limit_ms",
            "work_units",
            "evidence_hash",
        }
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("PHASE4EI_CANDIDATE_FIELDS_INVALID")
        identifier = row["candidate_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EI_CANDIDATE_ID_INVALID")
        seen.add(identifier)
        if not isinstance(row["evidence_hash"], str) or len(row["evidence_hash"]) != 64:
            raise ValueError("PHASE4EI_EVIDENCE_HASH_INVALID")
        try:
            int(row["evidence_hash"], 16)
        except ValueError as exc:
            raise ValueError("PHASE4EI_EVIDENCE_HASH_INVALID") from exc
        deadline = _timestamp(row["deadline"])
        observed = _timestamp(row["evidence_observed_at"])
        freshness_limit = _nonnegative_int(
            row["freshness_limit_ms"], "PHASE4EI_FRESHNESS_LIMIT_INVALID"
        )
        _nonnegative_int(row["work_units"], "PHASE4EI_WORK_INVALID", positive=True)
        deadline_slack = _milliseconds(deadline - as_of)
        evidence_age = _milliseconds(as_of - observed)
        freshness_remaining = freshness_limit - evidence_age
        reasons = []
        if deadline_slack < 0:
            reasons.append("DEADLINE_EXPIRED")
        if evidence_age < 0:
            reasons.append("EVIDENCE_FROM_FUTURE")
        elif freshness_remaining < 0:
            reasons.append("EVIDENCE_STALE")
        normalized = {
            **row,
            "deadline_slack_ms": deadline_slack,
            "evidence_age_ms": evidence_age,
            "freshness_remaining_ms": freshness_remaining,
        }
        if reasons:
            refused.append({**normalized, "status": "REFUSE", "reasons": reasons})
        else:
            scheduled.append({**normalized, "status": "SCHEDULED", "reasons": []})
    scheduled.sort(
        key=lambda row: (
            row["deadline_slack_ms"],
            row["freshness_remaining_ms"],
            row["candidate_id"],
        )
    )
    refused.sort(key=lambda row: row["candidate_id"])
    schedule = [{**row, "schedule_position": index} for index, row in enumerate(scheduled, start=1)]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EI",
        "input_hash": payload["artifact_hash"],
        "as_of": payload["as_of"],
        "ordering": ["DEADLINE_ASC", "FRESHNESS_REMAINING_ASC", "CANDIDATE_ID_ASC"],
        "schedule": schedule,
        "refused": refused,
        "scheduled_count": len(schedule),
        "refused_count": len(refused),
        "evaluations_executed": 0,
        "capital_reserved": False,
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
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    publish(args.output, report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
