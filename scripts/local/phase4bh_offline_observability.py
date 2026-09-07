"""Phase 4BH non-sensitive offline observability event validator."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.phase4cd.reconciliation_audit import canonical_hash

INPUT_SCHEMA = "phase4bh.observability-input.v1"
EVENT_SCHEMA = "phase4bh.offline-observability-event.v1"
STREAM_SCHEMA = "phase4bh.offline-observability-stream.v1"
MANIFEST_SCHEMA = "phase4bh.observability-safety-manifest.v1"
EVENT_TYPES = (
    "VALIDATION_STARTED",
    "VALIDATION_COMPLETED",
    "REFUSAL_STATE",
    "SIMULATION_STARTED",
    "ROLLBACK_VERIFIED",
    "ARTIFACT_PUBLISHED",
    "AUTHORIZATION_ABSENT_OR_EXPIRED",
    "SAFETY_INVARIANT_VIOLATION",
)
FORBIDDEN_KEYS = {
    "secret",
    "credential",
    "password",
    "token",
    "api_key",
    "sql",
    "database_path",
    "environment",
    "production_write_control",
    "service_command",
}
SQL_PATTERN = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", re.IGNORECASE)


def _hash(payload: dict[str, Any], field: str = "artifact_hash") -> str:
    return canonical_hash({key: value for key, value in payload.items() if key != field})


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("PHASE4BH_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("PHASE4BH_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None:
        raise ValueError("PHASE4BH_TIMESTAMP_TIMEZONE_MISSING")
    return parsed.astimezone(UTC)


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("PHASE4BH_INPUT_UNREADABLE") from exc
    if payload.get("schema") != INPUT_SCHEMA or payload.get("artifact_hash") != _hash(payload):
        raise ValueError("PHASE4BH_INPUT_SCHEMA_OR_HASH_INVALID")
    return payload


def build(input_path: Path, *, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    if now.tzinfo is None:
        raise ValueError("PHASE4BH_EVALUATION_TIMEZONE_MISSING")
    source = _load(input_path)
    events = source.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("PHASE4BH_EVENTS_MISSING")
    rows: list[dict[str, Any]] = []
    previous: datetime | None = None
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise ValueError("PHASE4BH_EVENT_SEQUENCE_INVALID")
        if set(event) - {
            "sequence",
            "event_type",
            "occurred_at",
            "phase",
            "subject_hash",
            "reason_codes",
            "attributes",
        }:
            raise ValueError("PHASE4BH_EVENT_FIELDS_INVALID")
        event_type = event.get("event_type")
        timestamp = _time(event.get("occurred_at"))
        if event_type not in EVENT_TYPES:
            raise ValueError("PHASE4BH_EVENT_TYPE_INVALID")
        if previous is not None and timestamp < previous:
            raise ValueError("PHASE4BH_EVENT_TIME_REGRESSION")
        previous = timestamp
        subject_hash = event.get("subject_hash")
        reasons = event.get("reason_codes")
        attributes = event.get("attributes")
        if not isinstance(subject_hash, str) or len(subject_hash) != 64:
            raise ValueError("PHASE4BH_SUBJECT_HASH_INVALID")
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise ValueError("PHASE4BH_REASON_CODES_INVALID")
        if not isinstance(attributes, dict):
            raise ValueError("PHASE4BH_ATTRIBUTES_INVALID")
        if any(str(key).lower() in FORBIDDEN_KEYS for key in attributes):
            raise ValueError("PHASE4BH_SENSITIVE_ATTRIBUTE_KEY")
        if any(
            not isinstance(value, (str, int, bool))
            or (isinstance(value, str) and (SQL_PATTERN.search(value) or "-----BEGIN" in value))
            for value in attributes.values()
        ):
            raise ValueError("PHASE4BH_SENSITIVE_ATTRIBUTE_VALUE")
        row = {
            "schema": EVENT_SCHEMA,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": timestamp.isoformat(),
            "phase": event.get("phase"),
            "subject_hash": subject_hash,
            "reason_codes": sorted(set(reasons)),
            "attributes": dict(sorted(attributes.items())),
            "contains_secrets": False,
            "contains_sql": False,
            "contains_credentials": False,
            "contains_production_write_controls": False,
        }
        row["event_hash"] = canonical_hash(row)
        rows.append(row)
    evaluated_at = now.astimezone(UTC).isoformat()
    stream: dict[str, Any] = {
        "schema": STREAM_SCHEMA,
        "phase": "4BH",
        "evaluated_at": evaluated_at,
        "input_hash": source["artifact_hash"],
        "event_count": len(rows),
        "events": rows,
        "events_hash": canonical_hash(rows),
        "database_mutation_performed": False,
        "execution_authorized": False,
    }
    stream["artifact_hash"] = _hash(stream)
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "phase": "4BH",
        "evaluated_at": evaluated_at,
        "stream_hash": stream["artifact_hash"],
        "allowed_event_types": list(EVENT_TYPES),
        "secrets_present": False,
        "sql_present": False,
        "credentials_present": False,
        "production_write_controls_present": False,
        "service_controls_present": False,
        "execution_authorized": False,
    }
    manifest["artifact_hash"] = _hash(manifest)
    return stream, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observability-input", type=Path, required=True)
    parser.add_argument("--evaluation-time", required=True)
    parser.add_argument("--stream-output", type=Path, required=True)
    parser.add_argument("--safety-manifest-output", type=Path, required=True)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.evaluation_time.replace("Z", "+00:00"))
    stream, manifest = build(args.observability_input, now=now)
    from phase4al_offline_protocol_simulation import publish_pair

    publish_pair(args.stream_output, args.safety_manifest_output, stream, manifest)
    print(json.dumps(stream, sort_keys=True))


if __name__ == "__main__":
    main()
