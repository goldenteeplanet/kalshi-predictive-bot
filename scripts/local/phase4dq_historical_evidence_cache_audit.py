"""Audit supplied historical evidence cache entries for no-lookahead correctness."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4dq.cache-audit-input.v1"
REPORT_SCHEMA = "phase4dq.cache-audit-report.v1"
ENTRY_SCHEMA = "phase4dq.evidence-entry.v1"
MAX_ENTRIES = 100_000


def _hash(payload: Any) -> str:
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key != "artifact_hash"}
    return canonical_hash(payload)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("PHASE4DQ_HASH_INVALID")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("PHASE4DQ_HASH_INVALID") from exc
    return value


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("PHASE4DQ_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("PHASE4DQ_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo != UTC:
        raise ValueError("PHASE4DQ_TIMESTAMP_INVALID")
    return parsed


def build_report(payload: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "decision_at",
        "max_age_seconds",
        "expected_source_id",
        "expected_series_id",
        "current_invalidation_generation",
        "entries",
        "artifact_hash",
    }
    if set(payload) != required:
        raise ValueError("PHASE4DQ_INPUT_FIELDS_INVALID")
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4DQ_INPUT_SCHEMA_OR_HASH_INVALID")
    decision_at = _time(payload["decision_at"])
    max_age = payload["max_age_seconds"]
    generation = payload["current_invalidation_generation"]
    if (
        not isinstance(max_age, int)
        or isinstance(max_age, bool)
        or max_age < 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        raise ValueError("PHASE4DQ_POLICY_INVALID")
    for field in ("expected_source_id", "expected_series_id"):
        if not isinstance(payload[field], str) or not payload[field]:
            raise ValueError("PHASE4DQ_EXPECTED_IDENTITY_INVALID")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise ValueError("PHASE4DQ_ENTRY_COUNT_INVALID")
    fields = {
        "schema",
        "entry_id",
        "source_id",
        "series_id",
        "observed_at",
        "available_at",
        "content_hash",
        "invalidation_generation",
        "artifact_hash",
    }
    identifiers: set[str] = set()
    decisions = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ValueError("PHASE4DQ_ENTRY_FIELDS_INVALID")
        identifier = entry["entry_id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("PHASE4DQ_ENTRY_ID_INVALID")
        identifiers.add(identifier)
        _digest(entry["content_hash"])
        artifact_valid = entry["artifact_hash"] == _hash(entry)
        observed = _time(entry["observed_at"])
        available = _time(entry["available_at"])
        if available < observed:
            raise ValueError("PHASE4DQ_ENTRY_TIMELINE_INVALID")
        entry_generation = entry["invalidation_generation"]
        if (
            not isinstance(entry_generation, int)
            or isinstance(entry_generation, bool)
            or entry_generation < 0
        ):
            raise ValueError("PHASE4DQ_INVALIDATION_GENERATION_INVALID")
        reasons = []
        if entry["schema"] != ENTRY_SCHEMA:
            reasons.append("SCHEMA_DRIFT")
        if not artifact_valid:
            reasons.append("ARTIFACT_HASH_MISMATCH")
        if entry["source_id"] != payload["expected_source_id"]:
            reasons.append("SOURCE_ID_MISMATCH")
        if entry["series_id"] != payload["expected_series_id"]:
            reasons.append("SERIES_ID_MISMATCH")
        if observed > decision_at:
            reasons.append("LOOKAHEAD_OBSERVATION")
        if available > decision_at:
            reasons.append("NOT_AVAILABLE_AT_DECISION")
        age = decision_at - observed
        age_microseconds = (age.days * 86_400 + age.seconds) * 1_000_000 + age.microseconds
        if age > timedelta(seconds=max_age):
            reasons.append("EVIDENCE_STALE")
        if entry_generation != generation:
            reasons.append("INVALIDATION_GENERATION_MISMATCH")
        decisions.append(
            {
                "entry_id": identifier,
                "eligible": not reasons,
                "age_microseconds": age_microseconds,
                "reasons": reasons,
            }
        )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "phase": "4DQ",
        "input_hash": payload["artifact_hash"],
        "entry_decisions": decisions,
        "eligible_entry_ids": sorted(row["entry_id"] for row in decisions if row["eligible"]),
        "ineligible_entry_ids": sorted(row["entry_id"] for row in decisions if not row["eligible"]),
        "no_lookahead_preserved": all(
            not row["eligible"]
            for row in decisions
            if {"LOOKAHEAD_OBSERVATION", "NOT_AVAILABLE_AT_DECISION"} & set(row["reasons"])
        ),
        "cache_entries_consumed": 0,
        "cache_entries_written": 0,
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
