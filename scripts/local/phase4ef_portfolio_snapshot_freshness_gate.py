"""Fail closed on stale, inconsistent, or mixed-version portfolio snapshots."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4ef.freshness-input.v1"
REPORT_SCHEMA = "phase4ef.freshness-report.v1"


def _hash(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key != "artifact_hash"}
    return canonical_hash(value)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4EF_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4EF_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("PHASE4EF_TIMESTAMP_INVALID")
    return parsed


def _nonnegative_int(value: Any, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(error)
    return value


def _milliseconds(delta: Any) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "as_of",
        "snapshot_version",
        "max_age_ms",
        "max_skew_ms",
        "fields",
        "artifact_hash",
    }
    if set(payload) != required:
        raise ValueError("PHASE4EF_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4EF_INPUT_SCHEMA_OR_HASH_INVALID")
    as_of = _timestamp(payload["as_of"])
    version = payload["snapshot_version"]
    if not isinstance(version, str) or not version:
        raise ValueError("PHASE4EF_VERSION_INVALID")
    max_age = _nonnegative_int(payload["max_age_ms"], "PHASE4EF_MAX_AGE_INVALID")
    max_skew = _nonnegative_int(payload["max_skew_ms"], "PHASE4EF_MAX_SKEW_INVALID")
    fields = payload.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("PHASE4EF_FIELDS_EMPTY")
    seen = set()
    normalized = []
    reasons = []
    observed_times = []
    for row in fields:
        if not isinstance(row, dict) or set(row) != {
            "field_id",
            "snapshot_version",
            "observed_at",
            "value_hash",
            "source_artifact_hash",
        }:
            raise ValueError("PHASE4EF_FIELD_FIELDS_INVALID")
        identifier = row["field_id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("PHASE4EF_FIELD_ID_INVALID")
        seen.add(identifier)
        if not _digest(row["value_hash"]) or not _digest(row["source_artifact_hash"]):
            raise ValueError("PHASE4EF_FIELD_HASH_INVALID")
        observed = _timestamp(row["observed_at"])
        observed_times.append(observed)
        age_ms = _milliseconds(as_of - observed)
        field_reasons = []
        if row["snapshot_version"] != version:
            field_reasons.append("MIXED_SNAPSHOT_VERSION")
        if age_ms < 0:
            field_reasons.append("FUTURE_OBSERVATION")
        elif age_ms > max_age:
            field_reasons.append("STALE_FIELD")
        reasons.extend(f"{identifier}:{reason}" for reason in field_reasons)
        normalized.append({**row, "age_ms": age_ms, "reasons": field_reasons})
    skew_ms = _milliseconds(max(observed_times) - min(observed_times))
    if skew_ms > max_skew:
        reasons.append("SNAPSHOT_OBSERVATION_SKEW_EXCEEDED")
    normalized.sort(key=lambda row: row["field_id"])
    reasons.sort(key=lambda reason: (reason.startswith("SNAPSHOT_"), reason))
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4EF",
        "input_hash": payload["artifact_hash"],
        "status": "FRESH_COHERENT_SNAPSHOT" if not reasons else "REFUSE",
        "eligible_for_risk_evaluation": not reasons,
        "as_of": payload["as_of"],
        "snapshot_version": version,
        "max_age_ms": max_age,
        "max_skew_ms": max_skew,
        "observed_skew_ms": skew_ms,
        "fields": normalized,
        "reasons": reasons,
        "risk_calculations_executed": 0,
        "risk_decisions_created": 0,
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
